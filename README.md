# Volve Drilling Advisor — AgentCore Edition

> Well 15/9-F-15 | Volve Field | Norwegian Continental Shelf

A production-grade AI drilling advisor deployed on **Amazon Bedrock AgentCore**, demonstrating the full architectural progression from raw API calls to a managed, serverless agent runtime with persistent memory and MCP tool connectivity.

---

## The Progression Story

This project is the third in a family of three implementations of the same Volve drilling advisor — each layer adding framework abstraction and managed infrastructure:

```
volve-drilling-advisor              # Raw Anthropic API
  └── Manual JSON schema, dispatcher, agent loop (~200 lines of boilerplate)

volve-drilling-advisor-strands      # Strands SDK
  └── @tool decorator + Agent() — framework collapses boilerplate to ~10 lines

volve-drilling-advisor-agentcore    # This repo — AgentCore Runtime
  └── Same Strands code, wrapped in BedrockAgentCoreApp, deployed to managed
      runtime with persistent session memory and MCP-connected tool
```

This progression is the interview narrative: **the same domain logic, three architectural layers.**

---

## What AgentCore Adds Over Strands

| Capability | Strands (local) | AgentCore |
|---|---|---|
| Runtime | Runs on your laptop | Managed microVM, scales automatically |
| Session memory | In-memory, lost on restart | Durable, persists across sessions |
| Long-running tasks | Limited by execution context | Up to 8 hours async |
| Observability | Manual logging | Native CloudWatch + OTEL traces |
| MCP tool connectivity | Manual wiring | Built-in Gateway for MCP servers |
| Deployment | You manage infra | `agentcore deploy` CLI or console |
| Auth/IAM | You handle it | Platform-enforced, IAM + Cognito |

---

## What Changed (Strands → AgentCore)

### Changed
| Component | Strands Edition | AgentCore Edition |
|---|---|---|
| Model client | `AnthropicModel` (direct API key) | `load_model()` via Amazon Bedrock (IAM-authenticated) |
| Agent lifecycle | Fresh agent per depth window (stateless) | Persistent agent per session (AgentCore Memory) |
| Execution | Synchronous `agent(prompt)` | Async streaming `agent.stream_async(prompt)` |
| Entry point | `run_advisor()` script loop | `@app.entrypoint` HTTP handler (serverless endpoint) |
| Memory | Manual reset to avoid `MaxTokensReachedException` | AgentCore Memory handles session state durably |

### What Did NOT Change
- All 4 `@tool` functions — **zero modification**
- `SYSTEM_PROMPT` — **verbatim carry-over**
- Data loading layer — **unchanged**

> This proves a key architectural point: **AgentCore wraps your agent, it doesn't rewrite it.**

---

## Architecture

```
Personal Mac (dev)          GitHub              AWS AgentCore
─────────────────           ──────              ─────────────────────────────
Write code (Strands)  →  git push  →  agentcore deploy
                                           │
                                           ├── Runtime (managed microVM)
                                           │     └── volve_drilling_advisor
                                           │           ├── main.py (@app.entrypoint)
                                           │           ├── 4 Strands @tool functions
                                           │           └── BedrockAgentCoreApp wrapper
                                           │
                                           ├── Memory (durable session state)
                                           │     └── venkat_volve_drilling_memory
                                           │           ├── Short-term (session)
                                           │           └── Long-term (cross-session)
                                           │
                                           └── Gateway (MCP endpoint)
                                                 └── venkat-volve-mcp-gateway
                                                       └── Silver table reader tool
```

---

## Agent Tools

All four tools carry over unchanged from the Strands edition:

### `get_formation_context(depth_m)`
Reads Silver formation tops and reservoir flags at the current depth. Returns formation position (Draupne/Hugin/below) and nearby HC potential flags.

### `get_drillability_forecast(depth_m)`
Returns HARD/MODERATE/SOFT drillability forecast from offset well analog data with geological basis for comparison against actual ROP.

### `check_rop_trend(window_index, current_depth)`
Compares current ROP against 5-window rolling trend. Detects ROP drops (>20% below trend) and determines if changes are formation-driven or bit-related.

### `check_mse_efficiency(wob_n, rpm, rop_mhr)`
Assesses bit efficiency using Mechanical Specific Energy (MSE) proxy:
```
MSE_proxy = WOB × RPM / ROP_mhr
```
Higher MSE = bit working harder for less penetration = inefficiency signal.

---

## Data Sources — Medallion Architecture

The Volve dataset uses a Bronze/Silver/Gold medallion architecture on Databricks:

| Layer | Source | Description |
|---|---|---|
| Bronze | Raw Equinor open data | Raw drilling parameters, well logs |
| Silver | `data/silver_formation_tops.csv` | Curated formation tops from offset wells |
| Silver | `data/silver_reservoir_flags.csv` | HC potential flags with severity ratings |
| Silver | `data/silver_drillability_forecast.csv` | Drillability forecast from offset analogs |
| Gold | `data/ROP data.csv` | Aggregated drilling parameters for F-15 |

> The Volve dataset is publicly available via Equinor's open data initiative.

---

## Key Field Context — Well 15/9-F-15

```
~3,350m  — Hugin reservoir entry (offset well analog)
 3,350m  — CRITICAL HC potential zone
 3,700m  — CRITICAL HC potential zone
 3,800–3,900m — Best confirmed reservoir (RHOB 2.31–2.32, RT 911–3,322 ohm.m)
 4,000–4,050m — CRITICAL anomaly (shale where offsets show HC sand)
```

---

## Deployment

### Prerequisites
- AWS account with Bedrock AgentCore access
- AgentCore CLI (`npm install -g @aws/agentcore`)
- Docker (for ARM64-compatible dependency packaging)
- Python 3.11+
- `uv` package manager

### Deploy
```bash
# Configure AWS credentials
aws configure sso   # or aws-azure-login for SAML-federated accounts

# Deploy to AgentCore Runtime
cd volveAgentcore
agentcore deploy
```

### Environment Variables
Set these in AgentCore Runtime → Advanced configurations:

| Variable | Description |
|---|---|
| `AWS_REGION` | `us-east-1` |
| `MEMORY_VOLVEAGENTCOREMEMORY_ID` | AgentCore Memory resource ID |
| `SILVER_TABLE_MCP_ENDPOINT` | AgentCore Gateway MCP endpoint URL |

---

## Testing

### Runtime Playground (AWS Console)
Navigate to AgentCore → Runtime playground → select `volve_drilling_advisor`:

```json
{
  "prompt": "Analyze drilling conditions at depth 3350m. This is a critical HC potential zone. Use your tools to investigate formation context, drillability forecast, ROP trend, and bit efficiency.",
  "window_index": 2
}
```

### AWS CLI
```bash
aws bedrock-agentcore invoke-agent-runtime \
  --agent-runtime-id volve_drilling_advisor \
  --runtime-session-id "session-001" \
  --payload '{"prompt": "What are the drilling conditions at 3350m?", "window_index": 2}'
```

---

## Dev Environment Notes

### Two Python versions coexist on Mac (normal)
```
/usr/bin/python3              → 3.9.6  (Apple system, restricted)
/opt/homebrew/bin/python3.11  → 3.11.x (Homebrew, use this)
```
Always verify with `which python3` before installing packages.

### CloudShell limitations
AWS CloudShell has a 1GB home directory limit — insufficient for AgentCore CLI (~466MB). Use local machine with Docker for deployments.

### SAML-federated AWS access
For SAML-federated sandbox accounts (Azure AD → AWS), use CloudShell for lightweight AWS CLI commands and deploy via console UI or local machine with Docker.

### ARM64 dependency packaging
AgentCore Runtime runs on Linux ARM64. When building the zip manually:
```bash
pip install \
  --platform manylinux_2_17_aarch64 \
  --target ./package \
  --implementation cp \
  --python-version 3.11 \
  --only-binary=:all: \
  <package>
```

---

## Observability

AgentCore provides native CloudWatch integration:
- **Logs**: Runtime → Endpoints → DEFAULT → Logs
- **Traces**: AgentCore → Assess → Observability
- **Dashboard**: Per-endpoint CloudWatch dashboard auto-created

---

## Interview Narrative

> "I have three versions of the same Volve drilling advisor. The first was manual — I wrote the JSON schema, dispatcher, and agent loop by hand, about 200 lines. The second ported it to Strands — the `@tool` decorator collapsed that to about 10 lines, and I ran it locally. The third deploys it to AgentCore — same Strands code, wrapped in `BedrockAgentCoreApp`, deployed to a managed runtime with persistent session memory and an MCP-connected tool. I can walk you through what changed at each layer and why."

This narrative covers: **Bedrock, Strands, AgentCore, MCP, session management, medallion architecture, and architectural decision-making** — all from one project family.

---

## Related Repositories

- [`volve-drilling-advisor`](https://github.com/venkatchittoor/volve-drilling-advisor) — Raw Anthropic API version
- [`volve-drilling-advisor-strands`](https://github.com/venkatchittoor/volve-drilling-advisor-strands) — Strands SDK version
- [`volve-drilling-advisor-agentcore`](https://github.com/venkatchittoor/volve-drilling-advisor-agentcore) — This repo

---

## License

MIT
