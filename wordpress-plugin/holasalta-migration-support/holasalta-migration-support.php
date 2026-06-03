<?php
/**
 * Plugin Name: HolaSalta Migration Support
 * Description: Registers REST meta fields and reduces unneeded intermediate image sizes during migration.
 * Version: 0.2.0
 * Author: HolaSalta
 */

if (!defined('ABSPATH')) {
    exit;
}

add_action('init', function () {
    $meta_fields = array(
        '_wix_id',
        '_wix_old_url',
        '_migration_batch',
    );

    foreach ($meta_fields as $field) {
        register_post_meta('post', $field, array(
            'single' => true,
            'type' => 'string',
            'show_in_rest' => true,
            'sanitize_callback' => 'holasalta_migration_sanitize_meta_value',
            'auth_callback' => 'holasalta_migration_meta_auth_callback',
        ));
    }
});

function holasalta_migration_sanitize_meta_value($value) {
    return is_scalar($value) ? sanitize_text_field((string) $value) : '';
}

function holasalta_migration_meta_auth_callback() {
    return current_user_can('edit_posts') || current_user_can('edit_others_posts') || current_user_can('manage_options');
}

add_action('rest_api_init', function () {
    register_rest_route('holasalta-migration/v1', '/meta-support', array(
        'methods' => 'GET',
        'permission_callback' => 'holasalta_migration_meta_auth_callback',
        'callback' => function () {
            $registered = function_exists('get_registered_meta_keys')
                ? get_registered_meta_keys('post', 'post')
                : array();
            $fields = array();

            foreach (array('_wix_id', '_wix_old_url', '_migration_batch') as $field) {
                $args = isset($registered[$field]) && is_array($registered[$field])
                    ? $registered[$field]
                    : array();
                $fields[$field] = array(
                    'registered' => !empty($args),
                    'single' => !empty($args['single']),
                    'type' => isset($args['type']) ? $args['type'] : '',
                    'show_in_rest' => !empty($args['show_in_rest']),
                    'auth_callback' => isset($args['auth_callback']) ? 'configured' : '',
                    'object_subtype' => isset($args['object_subtype']) ? $args['object_subtype'] : 'post',
                );
            }

            return array(
                'ok' => true,
                'plugin_active' => true,
                'post_type' => 'post',
                'fields' => $fields,
            );
        },
    ));
});

add_filter('intermediate_image_sizes_advanced', function ($sizes) {
    unset($sizes['medium_large']);
    unset($sizes['1536x1536']);
    unset($sizes['2048x2048']);

    return $sizes;
});
