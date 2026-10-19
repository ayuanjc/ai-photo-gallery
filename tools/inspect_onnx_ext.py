import onnx, onnxruntime
print("local ort", onnxruntime.__version__, "| onnx", onnx.__version__)
m = onnx.load("data/clip_text/model.onnx", load_external_data=False)
print("external tensors:", sum(1 for t in m.graph.initializer if len(t.external_data)))
seen = set()
for t in m.graph.initializer:
    for kv in t.external_data:
        seen.add((kv.key, kv.value if kv.key != "offset" else "N"))
print("locations:", [x for x in seen if x[0] in ("location",)][:5])
print("keys:", sorted({k for k, _ in seen}))
