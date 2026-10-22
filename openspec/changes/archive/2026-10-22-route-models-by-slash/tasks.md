## 1. Selector

- [x] 1.1 Replace catalog lookup in `apisix-models/routing_selector.lua` with `document.model:find("/", 1, true)`: slash → CPA with CPA key, otherwise AISIX with caller key.
- [x] 1.2 Rewrite `apisix-models/routing_selector_test.lua`: unknown bare IDs reach AISIX; any slash (including `/`, `//`, `/model`, `provider/`) reaches CPA; no index or cache access; validation and 16/64 MiB limits kept. Verified red on the old selector, red with a provider-format regex, green on the new selector.
- [x] 1.3 Deploy to production by in-place file write, `openresty -t` and reload, without restarting `apisix-models`.

## 2. Sub2API accounts (existing configuration only)

- [x] 2.1 Set `extra.openai_apikey_responses_websockets_v2_mode = http_bridge` on every OpenAI API-key account through the admin API.
- [x] 2.2 Replace the retired `http://cli-proxy-api:8317/rk/v1` `base_url` with `http://cli-proxy-api:8317/v1` on the seven accounts still using it.
- [x] 2.3 Verify with the installed Pi over WebSocket: bare Opus, bare GPT and `xl/claude-opus-5-5` return text; two-turn assistant-marker recall passes; AISIX logs show the bare-ID POSTs.

## 3. Documentation

- [x] 3.1 Record the rule, the account contract, the incident and the pre-push/pre-deploy checklist in `docs/model-routing.md`.
- [x] 3.2 Update README account table and cutover note.
