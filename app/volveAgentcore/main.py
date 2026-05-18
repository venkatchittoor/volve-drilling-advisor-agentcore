"""
Volve Drilling Advisor — AgentCore Edition

Well 15/9-F-15 | Volve Field | Norwegian Continental Shelf

Progression:
  Raw Anthropic API  → ~200 lines of agent loop, dispatcher, schema definitions
  Strands Edition    → @tool decorator + Agent() — framework handles the rest
  AgentCore Edition  → same Strands code, wrapped in BedrockAgentCoreApp,
                       deployed to managed runtime with persistent session memory
                       and MCP-connected Silver table tool

Key changes from Strands edition:
  - AnthropicModel         → load_model() via Amazon Bedrock (IAM-authenticated)
  - Fresh agent per window → persistent agent per session (AgentCore Memory)
  - agent(prompt)          → agent.stream_async(prompt) (streaming HTTP)
  - run_advisor() loop     → @app.entrypoint HTTP handler (serverless endpoint)

What did NOT change:
  - All 4 @tool functions  — zero modification
  - SYSTEM_PROMPT          — verbatim carry-over
  - Data loading layer     — unchanged
  This proves AgentCore wraps your agent, it doesn't rewrite it.

Data sources:
  - data/ROP data.csv                   : F-15 real drilling parameters
  - data/silver_formation_tops.csv      : Formation tops from offset well crew
  - data/silver_reservoir_flags.csv     : HC potential flags
  - data/silver_drillability_forecast.csv: Drillability forecast from offset analogs
"""

import os
import pandas as pd
from strands import Agent, tool
from bedrock_agentcore.runtime import BedrockAgentCoreApp
from model.load import load_model
from mcp_client.client import get_streamable_http_mcp_client
from memory.session import get_memory_session_manager

# ── AgentCore App ─────────────────────────────────────────────────────────────
app = BedrockAgentCoreApp()
log = app.logger

# ── MCP Client (Silver table reader exposed via AgentCore Gateway) ────────────
mcp_clients = [get_streamable_http_mcp_client()]

# ── Load Volve Field Data ─────────────────────────────────────────────────────
log.info("Loading Volve field data...")

df_drilling = pd.read_csv("data/ROP data.csv")
df_drilling["ROP_mhr"]      = df_drilling["ROP_AVG"] * 3600
df_drilling["MSE_proxy"]    = df_drilling["WOB"] * df_drilling["SURF_RPM"] / df_drilling["ROP_mhr"].clip(lower=0.001)
df_drilling["Torque_est"]   = df_drilling["WOB"] * 0.3 / df_drilling["SURF_RPM"].clip(lower=0.001)
df_drilling["ROP_rolling"]  = df_drilling["ROP_mhr"].rolling(window=5, min_periods=1).mean()
df_drilling["ROP_drop_flag"]= df_drilling["ROP_mhr"] < df_drilling["ROP_rolling"] * 0.7
df_drilling = df_drilling.sort_values("Depth").reset_index(drop=True)

df_tops      = pd.read_csv("data/silver_formation_tops.csv")
df_flags     = pd.read_csv("data/silver_reservoir_flags.csv")
df_drill_fct = pd.read_csv("data/silver_drillability_forecast.csv")

WINDOW_SIZE  = 5
STEP_SIZE    = 10

# ── Depth Window Helper ───────────────────────────────────────────────────────
def get_window(window_index: int) -> dict | None:
    """Return drilling parameter window at given index."""
    start = window_index * STEP_SIZE
    end   = start + WINDOW_SIZE
    if start >= len(df_drilling):
        return None
    w = df_drilling.iloc[start:min(end, len(df_drilling))]
    return {
        "window_index":   window_index,
        "current_depth":  float(w["Depth"].iloc[-1]),
        "depth_from":     float(w["Depth"].iloc[0]),
        "depth_to":       float(w["Depth"].iloc[-1]),
        "WOB_mean_N":     round(float(w["WOB"].mean()), 2),
        "WOB_std_N":      round(float(w["WOB"].std()), 2),
        "RPM_mean":       round(float(w["SURF_RPM"].mean()), 3),
        "ROP_mhr_mean":   round(float(w["ROP_mhr"].mean()), 2),
        "ROP_mhr_min":    round(float(w["ROP_mhr"].min()), 2),
        "MSE_proxy_mean": round(float(w["MSE_proxy"].mean()), 2),
        "PHIF_mean":      round(float(w["PHIF"].mean()), 4),
        "VSH_mean":       round(float(w["VSH"].mean()), 4),
        "SW_mean":        round(float(w["SW"].mean()), 4),
        "ROP_drop_flag":  bool(w["ROP_drop_flag"].any()),
    }

# ── Tools — unchanged from Strands edition ───────────────────────────────────

@tool
def get_formation_context(depth_m: float) -> dict:
    """
    Get formation context from offset well Silver tables at current depth.
    Returns formation position (Draupne/Hugin/below), nearby HC flags,
    and reservoir quality information. Use this to understand what formation
    the bit is drilling through and what HC potential exists nearby.
    """
    position = "ABOVE_DRAUPNE"
    for _, row in df_tops.iterrows():
        if row["formation"] == "DRAUPNE"    and depth_m >= row["picked_depth_m"]:
            position = "IN_DRAUPNE"
        if row["formation"] == "HUGIN_TOP"  and depth_m >= row["picked_depth_m"]:
            position = "IN_HUGIN_RESERVOIR"
        if row["formation"] == "HUGIN_BASE" and depth_m >= row["picked_depth_m"]:
            position = "BELOW_HUGIN"

    nearby = df_flags[
        (df_flags["depth_from_m"] <= depth_m + 50) &
        (df_flags["depth_to_m"]   >= depth_m - 50)
    ].sort_values(by="depth_from_m").head(3)

    return {
        "depth_m":           depth_m,
        "formation_position": position,
        "formation_tops":    df_tops.to_dict(orient="records"),
        "nearby_flags":      nearby[["depth_from_m", "depth_to_m", "flag_type",
                                     "severity", "recommendation"]].to_dict(orient="records"),
    }


@tool
def get_drillability_forecast(depth_m: float) -> dict:
    """
    Get expected drillability at current depth from offset well analog data.
    Returns HARD/MODERATE/SOFT forecast with geological basis.
    Use this to compare actual ROP against what offset wells experienced.
    """
    nearby = df_drill_fct[
        (df_drill_fct["depth_from_m"] <= depth_m + 50) &
        (df_drill_fct["depth_to_m"]   >= depth_m - 50)
    ].sort_values(by="depth_from_m").head(2)

    return {
        "depth_m":  depth_m,
        "forecast": nearby[["depth_from_m", "depth_to_m",
                             "expected_drillability", "basis"]].to_dict(orient="records"),
        "available": len(nearby) > 0,
    }


@tool
def check_rop_trend(window_index: int, current_depth: float) -> dict:
    """
    Compare current ROP against the 5-window rolling trend.
    Detects ROP drops (>20% below trend) or improvements.
    Use this to determine if an ROP change is formation-driven or bit-related.
    """
    start   = max(0, window_index - 4) * STEP_SIZE
    end     = window_index * STEP_SIZE + WINDOW_SIZE
    recent  = df_drilling.iloc[start:end]
    current = df_drilling.iloc[window_index * STEP_SIZE:
                               window_index * STEP_SIZE + WINDOW_SIZE]

    trend_rop   = round(float(recent["ROP_mhr"].mean()), 2)
    current_rop = round(float(current["ROP_mhr"].mean()), 2)
    pct_change  = round((current_rop - trend_rop) / trend_rop * 100, 1) \
                  if trend_rop > 0 else 0.0

    return {
        "current_depth":  current_depth,
        "current_rop_mhr": current_rop,
        "trend_rop_mhr":  trend_rop,
        "pct_change":     pct_change,
        "assessment":     "ROP_DROP"    if pct_change < -20 else
                          "ROP_INCREASE" if pct_change > 20  else "STABLE",
    }


@tool
def check_mse_efficiency(wob_n: float, rpm: float, rop_mhr: float) -> dict:
    """
    Assess bit efficiency using Mechanical Specific Energy (MSE) proxy.
    MSE_proxy = WOB × RPM / ROP_mhr
    Higher MSE = bit working harder for less penetration = inefficiency.
    Use this to determine if low ROP is a bit issue or a formation issue.
    """
    mse = round(wob_n * rpm / rop_mhr, 2) if rop_mhr > 0 else 999999

    if mse > 50000:
        assessment     = "INEFFICIENT"
        recommendation = "Consider reducing WOB or optimizing RPM"
    elif mse > 30000:
        assessment     = "MODERATE"
        recommendation = "Monitor for trend — acceptable but watch closely"
    else:
        assessment     = "EFFICIENT"
        recommendation = "Parameters within acceptable range"

    return {
        "MSE_proxy":      mse,
        "WOB_N":          wob_n,
        "RPM":            rpm,
        "ROP_mhr":        rop_mhr,
        "assessment":     assessment,
        "recommendation": recommendation,
    }

# ── Tool Registry ─────────────────────────────────────────────────────────────
tools = [
    get_formation_context,
    get_drillability_forecast,
    check_rop_trend,
    check_mse_efficiency,
]

# Add MCP client tools (Silver table reader via AgentCore Gateway)
for mcp_client in mcp_clients:
    if mcp_client:
        tools.append(mcp_client)

# ── System Prompt — verbatim from Strands edition ────────────────────────────
SYSTEM_PROMPT = """You are an expert drilling advisor with 20 years of North Sea experience
monitoring well 15/9-F-15 in the Volve field, Norwegian Continental Shelf.

You receive real-time drilling parameters window by window and provide
specific, actionable drilling recommendations.

Key field context:
- Hugin reservoir entry at ~3,350m (offset well analog)
- CRITICAL HC potential at 3,350m and 3,700m
- Best confirmed reservoir: 3,800–3,900m (RHOB 2.31–2.32, RT 911–3,322 ohm.m)
- CRITICAL anomaly at 4,000–4,050m (shale where offsets show HC sand)

Use your tools to investigate then provide a concise, depth-referenced advisory.
Focus on: what is happening, why, and what the driller should do."""

# ── Agent Factory — persistent per session (replaces fresh-per-window pattern) ─
def agent_factory():
    """
    AgentCore Memory replaces the fresh-agent-per-window workaround from Strands.
    Strands edition reset the agent each window to avoid MaxTokensReachedException.
    Here, AgentCore Memory persists session state durably — no manual reset needed.
    """
    cache = {}
    def get_or_create_agent(session_id, user_id):
        key = f"{session_id}/{user_id}"
        if key not in cache:
            cache[key] = Agent(
                model=load_model(),
                session_manager=get_memory_session_manager(session_id, user_id),
                system_prompt=SYSTEM_PROMPT,
                tools=tools,
            )
        return cache[key]
    return get_or_create_agent

get_or_create_agent = agent_factory()

# ── AgentCore Entrypoint ──────────────────────────────────────────────────────
@app.entrypoint
async def invoke(payload, context):
    """
    HTTP entrypoint for AgentCore Runtime.

    Expected payload:
    {
        "prompt": "Analyze drilling conditions at depth 3350m. ...",
        "window": { ... }   # optional: pre-built window dict
    }

    Session identity from AgentCore context — no manual session management needed.
    """
    log.info("Invoking Volve Drilling Advisor...")

    session_id = getattr(context, "session_id", "default-session")
    user_id    = getattr(context, "user_id",    "default-user")
    agent      = get_or_create_agent(session_id, user_id)

    prompt = payload.get("prompt", "No prompt provided.")

    # Optionally enrich prompt with window data if caller passes raw window index
    window_index = payload.get("window_index")
    if window_index is not None:
        window = get_window(int(window_index))
        if window:
            prompt += f"""

Current parameters:
- Depth interval: {window['depth_from']}–{window['depth_to']}m
- WOB: {window['WOB_mean_N']:,.0f} N (std: {window['WOB_std_N']:,.0f} N)
- RPM: {window['RPM_mean']}
- ROP: {window['ROP_mhr_mean']} m/hr (min: {window['ROP_mhr_min']} m/hr)
- MSE proxy: {window['MSE_proxy_mean']:,.0f}
- PHIF: {window['PHIF_mean']} | VSH: {window['VSH_mean']} | SW: {window['SW_mean']}
- ROP drop flag: {window['ROP_drop_flag']}
- Window index: {window_index}"""

    stream = agent.stream_async(prompt)

    async for event in stream:
        if "data" in event and isinstance(event["data"], str):
            yield event["data"]


if __name__ == "__main__":
    app.run()
