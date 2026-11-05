---
title: Decision Models administration
description: Configure compatible models and reusable Decision Profiles from System Settings.
sidebar:
  order: 4
---

# Decision Models administration

Open **System Settings → Decision Models** to manage reusable, structured decision configuration. This area keeps model configuration in the existing **Models** registry; it does not create a second model catalogue.

## Configure a decision model

Every model has a **Type**: **LLM** (chat and agents) or **Decision** (structured Decision Profiles only). A model is never both.

1. Open **System Settings → Models** and add a model under the provider that serves decisions (a SystemOne provider defaults to **Decision**).
2. Set **Type** to **Decision** and save. Chat-only fields (tokens, temperature, thinking, vision, global default) are hidden for decision models.

A decision model shows a **Decision** badge and supports `decision-schema/v1`. Only enabled decision models can be selected for a Decision Profile or for DMSS on the **Safety** page, and decision models never appear in agent or chat model pickers. To use the same provider model for both roles, add it twice: once as an LLM and once as a decision model.

### Use Cloudflare Workers AI Clef as a decision model

Clef (`@cf/cloudflare/clef`, and the cheaper `clef-flash`) is a decision model: it answers typed questions with probabilities, the same contract as SystemOne, so it is served by the **SystemOne** protocol adapter.

1. Add a provider with **Protocol Adapter** = *SystemOne* and **Endpoint** = `https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/ai/run/@cf/cloudflare/clef`, used exactly as written (nothing is appended). **API Key** is a Cloudflare API token with Workers AI access.
2. Add a model under it with **Type** = *Decision* and **Upstream Model ID** = `clef` or `clef-flash` (`@cf/cloudflare/clef` also works). That upstream id is what tells the adapter to speak Clef: it sends `model`, leaves out the fields Clef does not define, and reads answers inside Cloudflare's `{"result": ...}` wrapper. A score question is put to Clef as a list of levels (one per whole step of its bounds, otherwise five) and the answer is scaled back to the field's own bounds.
3. Pick the model as the primary (or fallback) of a profile, then try it in the **Playground**.

**Test connection** and **Fetch models** probe `<endpoint>/models`, which Clef's run URL does not have, so they report an error for this provider; this does not affect decisions. Use the Playground to check the provider. Clef bills input tokens only, and the token counts it returns are kept with each decision's provider metadata. Clef cannot report that it dropped part of a long `state`; the runtime's 64 KB request limit keeps requests well inside its 65,536-token window.

## Create or edit a profile

Under **Decision Models → Profiles**, select **New profile**. A profile has:

- an immutable lowercase, dot-separated key, such as `application.release_gate`;
- a display name and operator-facing description;
- an enabled switch and a mode: `off`, `shadow`, or `enforce`;
- a `decision-schema/v1` structured schema and a policy version;
- compatible primary and optional fallback models;
- timeout (1–300,000 ms), retry limit (0–10), and minimum confidence (0–1).

The form only lists compatible, enabled models for primary and fallback selection. It also reports server-side validation at the relevant field, including schema errors and concurrent-edit conflicts. Profiles retain a revision number so an older editor cannot silently overwrite a newer update.

## Retirement and deletion

Disable a profile and select `off` to safely retire it. Built-in profile keys cannot be deleted because consumers depend on their stable names; they can always be deactivated. Application-defined profiles can be deleted after confirmation.

## Playground and activity destinations

The **Playground** and **Activity** destinations are intentionally non-executing administration surfaces in this release. The **Open in Playground** action stores the selected profile/model in the browser navigation state and opens the destination without sending a provider request. It is safe to use when inspecting a configuration.

## Security

The settings page and profile API require an authenticated administrative session. Decision Profile responses contain model IDs only; provider API keys, OAuth tokens, and other credentials are never shown in profile views or selectors.
