"""人脸分类编排服务测试（注入 FakeFaceClient）。"""
from __future__ import annotations

from app.core.errors import VisionError
from app.models.face import FaceClassifyRequest
from app.services.face_service import FaceService, UnionFind


class FakeFaceClient:
    def __init__(self):
        self.detect_results: dict[str, list[dict]] = {}
        self.search_results: dict[str, list[dict]] = {}
        self._face_counter = 0
        self.created_sets: list[str] = []
        self.deleted_sets: list[str] = []
        self.detect_should_fail: set[str] = set()

    async def detect_faces(self, image_url: str, max_face: int = 10):
        if image_url in self.detect_should_fail:
            raise VisionError("detect boom")
        return self.detect_results.get(image_url, [])

    async def create_face_set(self, name: str, capacity: int = 100000):
        self.created_sets.append(name)
        return {"face_set_name": name}

    async def delete_face_set(self, name: str):
        self.deleted_sets.append(name)

    async def add_face_by_url(self, set_name: str, image_url: str, external_image_id: str):
        self._face_counter += 1
        return f"f{self._face_counter}"

    async def search_face_by_id(
        self, set_name: str, face_id: str, top_n: int = 10, threshold: float = 0.0
    ):
        return self.search_results.get(face_id, [])

    async def aclose(self):
        pass


def _bbox():
    return {"width": 100, "top_left_x": 50, "top_left_y": 50, "height": 120}


def _request(urls, **kw):
    payload = {"image_urls": urls}
    payload.update(kw)
    return FaceClassifyRequest(**payload)


# ---- UnionFind 测试 ----


def test_union_find_basic():
    uf = UnionFind(5)
    assert uf.find(0) != uf.find(1)
    uf.union(0, 1)
    assert uf.find(0) == uf.find(1)
    uf.union(1, 2)
    assert uf.find(0) == uf.find(2)
    assert uf.find(3) != uf.find(0)


def test_union_find_path_compression():
    uf = UnionFind(6)
    uf.union(0, 1)
    uf.union(1, 2)
    uf.union(2, 3)
    uf.union(3, 4)
    root = uf.find(4)
    assert uf.find(0) == root
    assert uf.find(2) == root


def test_union_find_no_op_same_set():
    uf = UnionFind(3)
    uf.union(0, 1)
    rank_before = uf._rank[:]
    uf.union(0, 1)
    assert uf._rank == rank_before


# ---- FaceService 测试 ----


async def test_no_faces_all_unclassified(settings):
    fc = FakeFaceClient()
    fc.detect_results = {"https://a.com/1.jpg": [], "https://a.com/2.jpg": []}
    svc = FaceService(settings, fc)
    resp = await svc.classify(_request(["https://a.com/1.jpg", "https://a.com/2.jpg"]))
    assert resp.total_images == 2
    assert resp.total_faces == 0
    assert resp.group_count == 0
    assert set(resp.unclassified_images) == {"https://a.com/1.jpg", "https://a.com/2.jpg"}


async def test_single_image_single_face(settings):
    fc = FakeFaceClient()
    fc.detect_results = {"https://a.com/1.jpg": [{"bounding_box": _bbox()}]}
    svc = FaceService(settings, fc)
    resp = await svc.classify(_request(["https://a.com/1.jpg"]))
    assert resp.total_faces == 1
    assert resp.group_count == 1
    assert resp.groups[0].face_count == 1
    assert resp.groups[0].image_count == 1
    assert resp.groups[0].representative_image_url == "https://a.com/1.jpg"


async def test_two_images_same_person(settings):
    fc = FakeFaceClient()
    fc.detect_results = {
        "https://a.com/1.jpg": [{"bounding_box": _bbox()}],
        "https://a.com/2.jpg": [{"bounding_box": _bbox()}],
    }
    fc.search_results = {
        "f1": [{"face_id": "f2", "similarity": 0.95}],
        "f2": [{"face_id": "f1", "similarity": 0.95}],
    }
    svc = FaceService(settings, fc)
    resp = await svc.classify(
        _request(["https://a.com/1.jpg", "https://a.com/2.jpg"], similarity_threshold=0.6)
    )
    assert resp.group_count == 1
    assert resp.groups[0].face_count == 2
    assert resp.groups[0].image_count == 2
    assert set(resp.groups[0].image_urls) == {"https://a.com/1.jpg", "https://a.com/2.jpg"}


async def test_two_images_different_persons(settings):
    fc = FakeFaceClient()
    fc.detect_results = {
        "https://a.com/1.jpg": [{"bounding_box": _bbox()}],
        "https://a.com/2.jpg": [{"bounding_box": _bbox()}],
    }
    fc.search_results = {
        "f1": [{"face_id": "f2", "similarity": 0.3}],
        "f2": [{"face_id": "f1", "similarity": 0.3}],
    }
    svc = FaceService(settings, fc)
    resp = await svc.classify(
        _request(["https://a.com/1.jpg", "https://a.com/2.jpg"], similarity_threshold=0.6)
    )
    assert resp.group_count == 2
    assert resp.groups[0].face_count == 1
    assert resp.groups[1].face_count == 1


async def test_one_image_two_faces_same_person(settings):
    fc = FakeFaceClient()
    fc.detect_results = {
        "https://a.com/1.jpg": [{"bounding_box": _bbox()}, {"bounding_box": _bbox()}],
    }
    fc.search_results = {
        "f1": [{"face_id": "f2", "similarity": 0.95}],
        "f2": [{"face_id": "f1", "similarity": 0.95}],
    }
    svc = FaceService(settings, fc)
    resp = await svc.classify(_request(["https://a.com/1.jpg"], similarity_threshold=0.6))
    assert resp.total_faces == 2
    assert resp.group_count == 1
    assert resp.groups[0].image_count == 1


async def test_detect_failure_degrades(settings):
    fc = FakeFaceClient()
    fc.detect_should_fail = {"https://a.com/1.jpg"}
    fc.detect_results = {"https://a.com/2.jpg": [{"bounding_box": _bbox()}]}
    svc = FaceService(settings, fc)
    resp = await svc.classify(_request(["https://a.com/1.jpg", "https://a.com/2.jpg"]))
    assert "https://a.com/1.jpg" in resp.unclassified_images
    assert any("检测" in w for w in resp.warnings)
    assert resp.total_faces == 1


async def test_face_set_deleted_after_classify(settings):
    fc = FakeFaceClient()
    fc.detect_results = {"https://a.com/1.jpg": [{"bounding_box": _bbox()}]}
    svc = FaceService(settings, fc)
    await svc.classify(_request(["https://a.com/1.jpg"]))
    assert len(fc.created_sets) == 1
    assert len(fc.deleted_sets) == 1
    assert fc.created_sets[0] == fc.deleted_sets[0]


async def test_groups_sorted_by_face_count_desc(settings):
    fc = FakeFaceClient()
    fc.detect_results = {
        "https://a.com/1.jpg": [{"bounding_box": _bbox()}, {"bounding_box": _bbox()}],
        "https://a.com/2.jpg": [{"bounding_box": _bbox()}],
    }
    svc = FaceService(settings, fc)
    resp = await svc.classify(
        _request(["https://a.com/1.jpg", "https://a.com/2.jpg"], similarity_threshold=0.99)
    )
    assert resp.group_count == 3
    for g in resp.groups:
        assert g.face_count == 1


async def test_return_face_boxes_false(settings):
    fc = FakeFaceClient()
    fc.detect_results = {"https://a.com/1.jpg": [{"bounding_box": _bbox()}]}
    svc = FaceService(settings, fc)
    resp = await svc.classify(_request(["https://a.com/1.jpg"], return_face_boxes=False))
    assert resp.groups[0].faces[0].bounding_box is None


async def test_return_face_boxes_true(settings):
    fc = FakeFaceClient()
    fc.detect_results = {"https://a.com/1.jpg": [{"bounding_box": _bbox()}]}
    svc = FaceService(settings, fc)
    resp = await svc.classify(_request(["https://a.com/1.jpg"], return_face_boxes=True))
    assert resp.groups[0].faces[0].bounding_box is not None
    assert resp.groups[0].faces[0].bounding_box.width == 100


async def test_dedup_urls(settings):
    fc = FakeFaceClient()
    fc.detect_results = {"https://a.com/1.jpg": [{"bounding_box": _bbox()}]}
    svc = FaceService(settings, fc)
    resp = await svc.classify(_request(["https://a.com/1.jpg", "https://a.com/1.jpg"]))
    assert resp.total_images == 1


async def test_confidence_single_face_is_one(settings):
    fc = FakeFaceClient()
    fc.detect_results = {"https://a.com/1.jpg": [{"bounding_box": _bbox()}]}
    svc = FaceService(settings, fc)
    resp = await svc.classify(_request(["https://a.com/1.jpg"]))
    assert resp.groups[0].confidence == 1.0


async def test_unclassified_images_no_face_set_created(settings):
    fc = FakeFaceClient()
    fc.detect_results = {"https://a.com/1.jpg": []}
    svc = FaceService(settings, fc)
    await svc.classify(_request(["https://a.com/1.jpg"]))
    assert len(fc.created_sets) == 0
    assert len(fc.deleted_sets) == 0
