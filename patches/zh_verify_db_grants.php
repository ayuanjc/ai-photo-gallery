<?php
define('WP_USE_THEMES', false);
require '/srv/zhihui-live/wp-load.php';
global $wpdb;
$r = array();
function t($name, $res) { global $r, $wpdb;
  $r[$name] = ($res === false) ? ('FAIL: ' . substr((string)$wpdb->last_error, 0, 90)) : 'ok'; }

$r['select_siteurl'] = $wpdb->get_var("SELECT option_value FROM {$wpdb->options} WHERE option_name='siteurl'");
t('create_temp', $wpdb->query("CREATE TEMPORARY TABLE zh_perm_tmp (id INT, v VARCHAR(8))"));
t('insert',       $wpdb->query("INSERT INTO zh_perm_tmp VALUES (1,'a')"));
t('update',       $wpdb->query("UPDATE zh_perm_tmp SET v='b' WHERE id=1"));
$r['update_val'] = $wpdb->get_var("SELECT v FROM zh_perm_tmp WHERE id=1");
t('delete',       $wpdb->query("DELETE FROM zh_perm_tmp WHERE id=1"));
t('drop_temp',    $wpdb->query("DROP TEMPORARY TABLE zh_perm_tmp"));
t('create_real',  $wpdb->query("CREATE TABLE IF NOT EXISTS zh_perm_probe (id INT PRIMARY KEY)"));
t('alter_real',   $wpdb->query("ALTER TABLE zh_perm_probe ADD COLUMN note VARCHAR(8)"));
t('index_real',   $wpdb->query("CREATE INDEX idx_note ON zh_perm_probe (note)"));
t('drop_real',    $wpdb->query("DROP TABLE zh_perm_probe"));
t('opt_add',      add_option('zh_perm_probe_opt', 'v1'));
t('opt_update',   update_option('zh_perm_probe_opt', 'v2'));
$r['opt_read'] = get_option('zh_perm_probe_opt');
t('opt_del',      delete_option('zh_perm_probe_opt'));
t('usermeta',     update_user_meta(1, 'zh_perm_probe_meta', 'x'));
$r['meta_read'] = get_user_meta(1, 'zh_perm_probe_meta', true);
delete_user_meta(1, 'zh_perm_probe_meta');
$r['users_can_register'] = get_option('users_can_register');
$r['wp_version'] = get_bloginfo('version');
echo json_encode($r, JSON_UNESCAPED_UNICODE | JSON_PRETTY_PRINT), "\n";
