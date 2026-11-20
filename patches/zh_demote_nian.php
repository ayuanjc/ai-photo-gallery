<?php
define('WP_USE_THEMES', false);
require '/srv/zhihui-live/wp-load.php';
$uid = 2;
$u = new WP_User($uid);
if (!$u->exists()) { echo "user missing\n"; exit(1); }
$before = implode(',', (array) $u->roles);
@mkdir('/srv/backup/wp', 0755, true);
file_put_contents('/srv/backup/wp/nian-role-20261120.txt',
  "user={$uid} login={$u->user_login} roles_before={$before} ts=" . date('c') . "\n");
echo "before roles: {$before}\n";
if ($before === 'zh_member') { echo "already zh_member, skip\n"; exit(0); }
$u->set_role('zh_member');
$u2 = new WP_User($uid);
echo "after  roles: " . implode(',', (array) $u2->roles) . "\n";
echo "posts still owned: " . count(get_posts(array('author' => $uid, 'post_type' => 'any',
  'post_status' => 'any', 'numberposts' => -1, 'fields' => 'ids'))) . "\n";
echo "can zh_manage: " . (user_can($uid, 'zh_manage') ? 'YES' : 'no') . "\n";
echo "can manage_options: " . (user_can($uid, 'manage_options') ? 'YES' : 'no') . "\n";
