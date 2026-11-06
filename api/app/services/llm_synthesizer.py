"""LLM Freedom Runway & Financial Independence Synthesizer for Plutus.

Grounded in peer-reviewed personal finance, portfolio math, and retirement literature:
1. Safe Withdrawal & Sequence of Returns (William Bengen 1994, Trinity Study 1998):
   4% safe withdrawal floor vs perpetual growth threshold.
2. Dynamic Burn Titration & Guardrails (Michael Kitces, Guyton-Klinger):
   Titrating discretionary spending downwards during market drawdowns extends runway non-linearly.
3. Liquidity Waterfall (Asset Allocation & Asset Location):
   Cash floor (emergency buffer) -> Growth index equities -> Locked pension reserve (KiwiSaver).
4. Freedom Horizon Expansion:
   Structural reductions in weekly burn yield exponential increases in autonomy weeks.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.request
from typing import Any, Optional

from app.config import settings
from app.schemas import RunwayAiSynthesisRead
from app.services.runway import calculate_compounding_runway

logger = logging.getLogger(__name__)

USER_AGENT = "Yggdrasil-Plutus/1.0 (PlutusSynthesizer)"

SYSTEM_PROMPT = """You are an expert quantitative wealth coach and financial autonomy scientist advising an individual on financial freedom runway.
Your recommendations are grounded in four scientific pillars:
1. Safe Withdrawal & Sequence of Returns (Bengen 1994, Trinity Study): Evaluate liquid net worth against the 4% safe withdrawal floor and perpetual growth threshold.
2. Dynamic Burn Titration (Kitces / Guyton-Klinger): Recommend practical adjustments to discretionary burn that protect capital without excessive lifestyle deprivation.
3. Liquidity Waterfall (Asset Location): Ensure an adequate cash floor (minimum 12-26 weeks of living expenses) before aggressively relying on volatile growth equities.
4. Freedom Horizon Expansion: Explain how modest spending adjustments compound into years of freedom and autonomy.

You must respond ONLY with a valid JSON object matching this exact structure:
{
  "headline": "string (punchy, high-signal verdict, e.g. 'Runway extends +18 weeks with $100/w burn titration')",
  "recommendations": [
    "string (actionable, high-leverage step 1)",
    "string (actionable, high-leverage step 2)",
    "string (actionable, high-leverage step 3)"
  ],
  "rationale": "string (concise synthesis of liquid assets, burn rate, and athlete/user context)",
  "research_citation": "string (e.g. 'Trinity Study (Safe Withdrawal) & Kitces (Dynamic Burn Titration)')",
  "impact_summary": "string (e.g. 'Liquid runway: 142 weeks, cash floor secured, perpetual threshold within reach')"
}
"""


def synthesize_runway_deterministic(
    runway_data: dict[str, Any],
    simulated_weeks: Optional[float],
    simulated_years: Optional[float],
    is_perpetual: bool,
    delta_weeks: Optional[float],
    custom_context: Optional[str] = None,
) -> RunwayAiSynthesisRead:
    """Deterministic fallback synthesis when Muninn LLM gateway is offline."""
    burn = float(runway_data.get("weekly_burn_rate", 1000.0))
    liquid = float(runway_data.get("total_liquid_balance", 0.0))

    if is_perpetual:
        headline = "Perpetual Autonomy Achieved: Asset Growth Outpaces Total Weekly Burn"
        recs = [
            "Maintain disciplined asset allocation across low-cost growth index funds.",
            "Keep emergency cash floor buffered to prevent selling growth assets during market corrections.",
            "Titrate discretionary lifestyle spending while remaining well above perpetual yield floor.",
        ]
        impact = f"Total liquid capital ${liquid:,.0f} generates sufficient yield to cover ${burn:,.0f}/w indefinitely."
    elif simulated_weeks and simulated_weeks > 52:
        headline = f"Strong Financial Runway: {simulated_weeks:.0f} Weeks of Full Living Autonomy"
        recs = [
            "Titrate discretionary spending to compound cash reserves into growth equities.",
            "Preserve minimum 26 weeks of liquid cash buffer before deploying surplus.",
            "Review recurring subscriptions and transportation expenses for low-friction burn reduction.",
        ]
        impact = f"Runway covers {simulated_years:.1f} years of independent living at current ${burn:,.0f}/w burn."
    else:
        headline = f"Runway Alert: {simulated_weeks or 0:.0f} Weeks Available at Current Burn"
        recs = [
            "Initiate dynamic burn titration to preserve liquid cash floor.",
            "Postpone non-essential capital outlays until emergency runway exceeds 26 weeks.",
            "Audit weekly discretionary food and retail spending to reclaim freedom weeks.",
        ]
        impact = f"Immediate burn reduction of $150/w would add significant autonomy buffer."

    rationale = f"Synthesized from total liquid assets (${liquid:,.0f}) and weekly burn (${burn:,.0f}/w)."
    if custom_context:
        rationale += f" Evaluated against user goals: {custom_context.strip()}."

    return RunwayAiSynthesisRead(
        headline=headline,
        recommendations=recs,
        rationale=rationale,
        research_citation="Trinity Study (Safe Withdrawal) & Kitces (Dynamic Burn Titration)",
        impact_summary=impact,
        is_llm_generated=False,
        simulated_runway_weeks=simulated_weeks,
        simulated_runway_years=simulated_years,
        is_perpetual=is_perpetual,
        delta_weeks_compared_to_baseline=delta_weeks,
    )


def synthesize_runway_scenario_llm(
    runway_data: dict[str, Any],
    burn_adjustment: float = 0.0,
    annual_return: Optional[float] = None,
    lump_sum: float = 0.0,
    custom_context: Optional[str] = None,
) -> RunwayAiSynthesisRead:
    """Synthesizes research-grounded financial runway insights and recommendations via Muninn LLM."""
    baseline_liquid = float(runway_data.get("total_liquid_balance", 0.0))
    baseline_burn = float(runway_data.get("weekly_burn_rate", 1000.0))
    baseline_return = 0.05

    simulated_burn = max(1.0, baseline_burn + burn_adjustment)
    simulated_liquid = max(0.0, baseline_liquid + lump_sum)
    simulated_return = annual_return if annual_return is not None else baseline_return

    sim_weeks, is_perpetual = calculate_compounding_runway(
        accessible_assets=simulated_liquid,
        weekly_burn=simulated_burn,
        annual_real_return=simulated_return,
    )
    sim_years = round(sim_weeks / 52.0, 2) if sim_weeks is not None else None

    # Calculate baseline weeks for delta
    base_weeks, base_perpetual = calculate_compounding_runway(
        accessible_assets=baseline_liquid,
        weekly_burn=baseline_burn,
        annual_real_return=baseline_return,
    )
    delta_weeks = None
    if not is_perpetual and not base_perpetual and sim_weeks is not None and base_weeks is not None:
        delta_weeks = round(sim_weeks - base_weeks, 2)

    try:
        context_payload = {
            "financial_telemetry": {
                "cash_balance_nzd": runway_data.get("cash_balance", 0.0),
                "growth_balance_nzd": runway_data.get("growth_balance", 0.0),
                "locked_balance_nzd": runway_data.get("locked_balance", 0.0),
                "total_liquid_nzd": baseline_liquid,
                "current_weekly_burn_nzd": baseline_burn,
                "emergency_weeks": runway_data.get("emergency_weeks", 0.0),
                "growth_weeks": runway_data.get("growth_weeks"),
                "is_perpetual": runway_data.get("is_perpetual", False),
            },
            "scenario_adjustments": {
                "burn_adjustment_per_week": burn_adjustment,
                "effective_simulated_burn": simulated_burn,
                "lump_sum_adjustment": lump_sum,
                "effective_simulated_liquid": simulated_liquid,
                "expected_real_annual_return": simulated_return,
                "simulated_runway_weeks": sim_weeks,
                "simulated_runway_years": sim_years,
                "is_perpetual": is_perpetual,
                "delta_weeks": delta_weeks,
            },
            "user_custom_instructions": custom_context or "None provided. Synthesize optimal runway preservation and freedom strategy.",
        }

        url = f"{settings.llm_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {settings.llm_api_key}",
            "User-Agent": USER_AGENT,
        }
        body = {
            "model": settings.llm_model,
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": f"Synthesize athlete/user financial runway scenario:\n{json.dumps(context_payload, indent=2)}",
                },
            ],
        }

        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=12.0) as resp:
            if resp.status == 200:
                resp_json = json.loads(resp.read().decode("utf-8"))
                choices = resp_json.get("choices", [])
                if choices:
                    content_str = choices[0].get("message", {}).get("content", "")
                    content_data = json.loads(content_str)
                    return RunwayAiSynthesisRead(
                        headline=content_data.get("headline", "Autoregulated Financial Freedom Scenario"),
                        recommendations=content_data.get("recommendations", []),
                        rationale=content_data.get("rationale", "Synthesized from asset balances and burn rate."),
                        research_citation=content_data.get("research_citation", "Trinity Study (Safe Withdrawal) & Kitces (Dynamic Burn Titration)"),
                        impact_summary=content_data.get("impact_summary", "Runway projection synthesized."),
                        is_llm_generated=True,
                        simulated_runway_weeks=sim_weeks,
                        simulated_runway_years=sim_years,
                        is_perpetual=is_perpetual,
                        delta_weeks_compared_to_baseline=delta_weeks,
                    )
    except Exception as exc:
        logger.warning("LLM runway synthesis failed or timed out (%s); falling back to deterministic synthesis", exc)

    return synthesize_runway_deterministic(
        runway_data=runway_data,
        simulated_weeks=sim_weeks,
        simulated_years=sim_years,
        is_perpetual=is_perpetual,
        delta_weeks=delta_weeks,
        custom_context=custom_context,
    )
