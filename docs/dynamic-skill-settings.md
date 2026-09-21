# Dynamic skill settings

Evonic skill manifests can opt into a provider-first settings layout without creating a bespoke settings page. The shared renderer in `templates/skill_detail.html` keeps ordinary skills on the existing static field layout and uses provider-first rendering only when a manifest declares it.

## Provider-first layout

Add a `settings_ui` object to `skill.json`:

```json
{
  "settings_ui": {
    "layout": "provider-first",
    "provider_selector": {
      "label": "Provider",
      "description": "Choose the provider to configure.",
      "empty_label": "— Select a provider —",
      "providers": [
        {"id": "example-cloud", "label": "Example Cloud"},
        {"id": "example-local", "label": "Example Local"}
      ]
    },
    "global_section": {
      "label": "Global defaults"
    }
  }
}
```

Annotate each variable with one of the following UI metadata fields:

- `provider`: provider ID that owns the field. It appears only while that provider is selected.
- `section: "global"`: a shared setting shown in the global defaults section.
- `advanced: true`: an optional field displayed inside the Advanced settings disclosure.
- `required: true`: an optional UI-only input to the provider configuration status badge.
- Existing `label`, `description`, `default`, `type`, `options`, `options_source`, and `empty_label` metadata continues to work.

Example:

```json
{
  "name": "example_cloud_model",
  "label": "Model",
  "type": "select",
  "provider": "example-cloud",
  "options": [{"value": "v1", "label": "Version 1"}]
}
```

## Behavior

- The provider selector is rendered before provider fields.
- Switching providers captures current form values before the DOM is rerendered, so unsaved values remain available when users switch back.
- Only the selected provider's settings are visible; global settings are presented separately.
- Saving includes all declared variables, including values for providers that are not currently selected.
- Providers with no selected value show an intentional empty state.
- Provider status is a UI indication based on variables marked `required: true`. The Image Generator manifest currently does not use required-field metadata, so its status is informational until provider-specific validation is added.

## Scope

This metadata controls rendering and client-side form state only. It does not change provider execution, connection testing, credential storage, secret treatment, API contracts, or server-side validation. Those concerns must be implemented independently.
