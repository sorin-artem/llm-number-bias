# LLM Number Bias

**Ask the same question. Keep every answer. See the distribution.**

> Pick a number between 1 and 10

A small Python script for comparing how LLMs answer this question through [OpenRouter](https://openrouter.ai). No UI, database, or LangChain. Every request starts a fresh conversation. You get full response logs, a CSV, a summary, and a PNG chart.

## Quick start

Requires **Python 3.11+** and an OpenRouter API key with credits.

```bash
git clone https://github.com/sorin-artem/llm-number-bias.git
cd llm-number-bias
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Edit `.env` locally:

```dotenv
OPENROUTER_API_KEY=your-openrouter-key
```

Run **10 requests per model**, 50 requests in total:

```bash
python pick_number.py run --samples 10
```

The terminal prints each response and the output directory. The key is never intentionally logged; `.env` and ordinary results are excluded from Git. An exported `OPENROUTER_API_KEY` takes precedence over `.env`; `OPEN_ROUTER_API_KEY` is also accepted for compatibility.

## Example chart

![Real number-picking results: ten independent requests per model](examples/2026-10-21/distribution.png)

**Real run on October 21, 2026:** 50 independent requests, 50 valid answers, **$0.063070** total reported cost. Reasoning `low`, output limit 1024, provider-default temperature.

| Model | Picked 7 | Picked 8 | Other numbers |
| --- | ---: | ---: | ---: |
| GPT-6.1 Sol | 10 | 0 | 0 |
| Claude Opus 5.5 | 10 | 0 | 0 |
| Grok 4.7 | 10 | 0 | 0 |
| Gemini 4 Argon | 10 | 0 | 0 |
| Muse Wonder | 9 | 1 | 0 |

These are observations from this small run, not claims about long-run probabilities. Inspect the [full responses](examples/2026-10-21/responses.jsonl), [CSV](examples/2026-10-21/responses.csv), [exact configuration](examples/2026-10-21/config.json), and [summary with costs](examples/2026-10-21/summary.json).

You can rebuild this example without an API key:

```bash
python pick_number.py plot examples/2026-10-21
```

## Models and settings

The initial model set was checked against the [OpenRouter model catalog](https://openrouter.ai/api/v1/models) on **October 21, 2026**:

| Provider | Model ID |
| --- | --- |
| OpenAI | `openai/gpt-6.1-sol` |
| Anthropic | `anthropic/claude-opus-5.5` |
| xAI | `x-ai/grok-4.7` |
| Google | `google/gemini-4-argon` |
| Meta | `meta/muse-wonder` |

Defaults: reasoning **low**, output limit **1024 tokens**, no explicit temperature, no fixed seed, no tools, no system message, and no history. All five models advertised support for `low` when checked. The same effort label does not imply the same reasoning budget across providers.

```bash
# Increase the sample size
python pick_number.py run --samples 100

# Pick exact models
python pick_number.py run --models openai/gpt-6.1-sol anthropic/claude-opus-5.5 --samples 20

# Change the experiment explicitly
python pick_number.py run --prompt "Pick a number between 1 and 10. Reply with only the number." --reasoning medium --max-tokens 2048

# Regenerate reports from any saved run; makes no API requests
python pick_number.py plot results/YOUR_RUN_ID
```

Use `python pick_number.py run --help` for timeout and output-directory options. Chosen models must support the supplied reasoning setting. Unsupported parameters produce a recorded error, rather than silently dropping the setting.

## What gets saved

Each run creates `results/<UTC timestamp>-<short ID>/`:

| File | Contents |
| --- | --- |
| `config.json` | Exact settings, run ID, timestamps, and completion state |
| `responses.jsonl` | One record per attempted request, including the complete SDK response payload on success |
| `responses.csv` | Answers, numbers, statuses, latency, token usage, reported cost, response IDs, and trace IDs |
| `summary.json` | Counts, percentages, status totals, and reported cost coverage per model |
| `distribution.png` | One panel per model, shared percentage scale, and a 10% uniform reference line |

The model order rotates round-robin: one request to each model, then the next round. Requests are sequential. SDK retries are disabled, and the script does not request a fallback model. OpenRouter may route between providers serving the same model; the returned model and provider are logged when available.

An answer is valid if the completed visible text contains **exactly one numeric expression**, which is an unsigned integer from **1 through 10**. `I'll pick 7.` counts; `7 or 8`, `Between 1 and 10, I choose 7`, decimal numbers, and spelled-out numbers do not. Empty, truncated, incomplete, ambiguous, unparsed, out-of-range answers and API errors are retained separately. No additional model is used to interpret responses.

Percentages use **valid answers per model** as the denominator. Panels also show recorded/planned counts and non-valid totals. A model with no valid answers is explicitly marked.

Every JSONL record is flushed to disk immediately. **Ctrl+C** saves reports for responses already received; an interrupted in-flight request has unknown cost and may still be billed. Authentication, credit, and permission errors stop the run. Other API errors are logged and the run continues without retrying. Exit codes: `0` all answers valid, `1` at least one non-valid result, `2` CLI/configuration error, `130` interrupted.

## Cost and interpretation

Reasoning tokens are billed even when the visible answer is a single digit, and generally share the output-token limit. At the catalog rates checked on October 21, 2026, 10 requests to each default model cost about **$0.07 with 100 output/reasoning tokens per request**, or **$0.68 if every request uses all 1024 output tokens**, assuming roughly 20 input tokens each. These are estimates, not a spending guarantee; prices, provider routing, and token accounting can change. No retries are included.

The 20-input-token assumption above is illustrative: billed counts can differ substantially. For example, Grok reported 1,251 input tokens per request in the recorded run, even though the script supplied just one short user message.

The report sums `usage.cost` returned by OpenRouter. Missing costs remain unknown, not zero; the summary and chart show how many records include cost. Check your OpenRouter Activity for billing, especially after errors or interruptions.

**Ten samples are a smoke test, not evidence that a model is or is not random.** Results describe these API settings and provider routes, not necessarily the providers' chat products. Prompt wording, reasoning settings, model updates, and routing can change the distribution. This is an exploratory tool, not a model-quality benchmark or a random-number generator.

## Optional Langfuse / Broadcast

Local logs and charts work with **only an OpenRouter key**. You do not need a Langfuse account or keys in the script.

To additionally inspect calls in Langfuse:

1. Create a Langfuse project and a Public/Secret API key pair in **Settings → API Keys**.
2. In your own OpenRouter account, open **Settings → Observability** and enable **Broadcast / Send traces**.
3. Add a **Langfuse** destination. Enter the key pair and your project's region URL, such as `https://cloud.langfuse.com` (EU) or `https://us.cloud.langfuse.com` (US). Leave custom headers empty for Langfuse Cloud.
4. Test the connection and save it. Send a test trace and verify it in Langfuse **Tracing**.
5. Keep destination **Privacy Mode off** if you want prompt and completion text; use 100% sampling to see every call.

The script supplies `trace_id`, `trace_name`, `run_id`, `sample`, and `requested_model` in OpenRouter's trace metadata so calls can be identified alongside local records. Broadcast is configured per user's OpenRouter account; cloning this repository does not inherit anyone else's destination. Other Broadcast destinations can be used instead. No Langfuse SDK is installed, avoiding duplicate SDK/Broadcast traces.

See [OpenRouter → Langfuse setup](https://openrouter.ai/docs/guides/features/broadcast/langfuse), [Langfuse's OpenRouter integration](https://langfuse.com/integrations/gateways/openrouter), and [reasoning token accounting](https://openrouter.ai/docs/guides/best-practices/reasoning-tokens).

## Tests

```bash
python -m unittest discover -s tests -v
```

Tests use mocked responses and temporary directories. They do not use your API key, incur charges, or send traces.
