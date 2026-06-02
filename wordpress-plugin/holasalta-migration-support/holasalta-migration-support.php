<?php
/**
 * Plugin Name: HolaSalta Migration Support
 * Description: Registers REST meta fields and reduces unneeded intermediate image sizes during migration.
 * Version: 0.1.0
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
            'sanitize_callback' => 'sanitize_text_field',
            'auth_callback' => function () {
                return current_user_can('edit_posts');
            },
        ));
    }
});

add_filter('intermediate_image_sizes_advanced', function ($sizes) {
    unset($sizes['medium_large']);
    unset($sizes['1536x1536']);
    unset($sizes['2048x2048']);

    return $sizes;
});
