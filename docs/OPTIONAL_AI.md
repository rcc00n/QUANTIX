# Optional explanatory analysis

NVIDIA is optional and disabled by default. The scanner, sizing, risk decisions,
paper coordinator, portfolio, reporting and exports do not depend on it. No external
AI request was authorized or made during this implementation. No local GPU or model
server is required.

Authenticated `GET /api/v1/ai/status` reports the actual gate state.
`POST /api/v1/ai/explain` accepts exactly one retained `opportunity_id` or owned
`session_id`. It loads actual numerical results server-side. There is no arbitrary
prompt, report upload, tool, accounting writer or strategy-changing interface.
Disabled and failed providers return useful deterministic explanations with the
exact source metrics and historical attribution. Null values remain unavailable;
zero completed simulations explicitly provides no evidence of profitability.

External use requires all of `AI_ENABLED=true`, a separately provided
`NVIDIA_API_KEY`, an explicit model, `AI_FREE_ALLOWANCE_AUTHORIZED=true`, and a future
UTC `AI_FREE_ALLOWANCE_EXPIRES_AT`. The authorization is a separate owner decision
confirming a usable free allowance, not an assertion that NVIDIA pricing or credits
are automatically known. There is no paid-usage authorization path, automatic
upgrade or fallback provider. Missing or expired authorization, untrusted application
UTC, missing configuration or provider failure prevents a network request and keeps
the deterministic explanation available. Credentials never enter prompts, responses,
system events or exports.

The provider implements the documented NVIDIA HTTPS chat completions endpoint,
with no redirects, environment proxies or configurable host. See NVIDIA's official
[LLM API reference](https://docs.api.nvidia.com/nim/reference/llm-apis).

Defaults are at most 20 requests per UTC day, 60 seconds between attempts, 300-second
caching, 10-second timeout, 12,000 input bytes, 400 output tokens, and a 32-KiB response
read limit. A single in-flight request prevents queue growth; cache entries are
bounded to 64. Durable `ai.provider_attempt` events and an owner row lock preserve
daily and frequency limits across restarts. Cached explanations do not consume the
durable attempt quota. No response text is persisted to financial history.

Only allowlisted exact numeric strings, classification and known deterministic
reason codes enter the prompt. Exchange descriptions, market symbols, owner names,
free text, URLs and config/evidence dumps are excluded. The model may return only a
unique bounded list of existing explanation IDs. The application renders its own
server-authored explanations in that order and always retains the paper-only and
historical qualifications. Generated prose, new numbers, unknown IDs, extra fields,
function calls and tool calls are rejected. This constraint makes external analysis
a prioritization aid without giving model text financial authority.

Synthetic tests exercise disabled/missing key/allowance/expiry/time gates, rate and
daily limits, cache expiration, timeout/failure, malformed or injected output,
secret exclusion, strict request schemas and authenticated API access. They make
zero real NVIDIA calls and do not prove current NVIDIA free-credit availability.
