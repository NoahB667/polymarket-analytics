import time
from typing import Dict, Any, List

class WalletIntelligence:
    """
    Pure statistical intelligence layer that calculates aggregate behavioral 
    profiles and extracts a normalized 0.0 to 1.0 Suspicion Score for individual addresses.
    
    Isolated from I/O to allow seamless reuse in both bulk processing and live streaming hooks.
    """
    @staticmethod
    def compile_profile(wallet_address: str, trades: List[Dict[str, Any]]) -> Dict[str, Any]:
        """
        Aggregates raw on-chain transaction history into profile metrics.
        """
        if not trades:
            return {}

        total_trades = len(trades)
        distinct_markets = len(set(t["slug"] for t in trades))
        
        # Parse global sizing scales
        total_usd_volume = sum(float(t.get("usd_volume", 0.0)) for t in trades)
        average_position_size = total_usd_volume / total_trades if total_trades > 0 else 0.0

        # Calculate chronological baseline matrix
        timestamps = [float(t["block_timestamp"]) for t in trades]
        first_trade_time = min(timestamps)
        account_age_days = max(0.1, (time.time() - first_trade_time) / 86400.0)

        # Statistical extraction tracking vectors
        long_shot_attempts = 0
        long_shot_wins = 0
        category_counts: Dict[str, int] = {}

        for trade in trades:
            # Group sectors to locate concentrated topic tracking
            slug = str(trade.get("slug", "")).lower()
            category = "other"
            for sector in ["fed", "rate", "election", "crypto", "sec", "peace", "israel", "iran", "taiwan"]:
                if sector in slug:
                    category = sector
                    break
            category_counts[category] = category_counts.get(category, 0) + 1

            # Long-shot metric evaluation (odds at entry <= 20 cents)
            if float(trade.get("entry_price", 1.0)) <= 0.20:
                long_shot_attempts += 1
                if trade.get("outcome") == trade.get("resolved_outcome") and trade.get("resolved_outcome") is not None:
                    long_shot_wins += 1

        # Calculate behavioral ratios safely
        long_shot_win_rate = long_shot_wins / long_shot_attempts if long_shot_attempts > 0 else 0.0
        
        max_category_volume = max(category_counts.values()) if category_counts else 0
        category_concentration = max_category_volume / total_trades if total_trades > 0 else 0.0

        return {
            "wallet_address": wallet_address,
            "total_trades": total_trades,
            "distinct_markets": distinct_markets,
            "long_shot_attempts": long_shot_attempts,
            "long_shot_wins": long_shot_wins,
            "long_shot_win_rate": round(long_shot_win_rate, 4),
            "category_concentration": round(category_concentration, 4),
            "account_age_days": round(account_age_days, 2),
            "average_position_size": round(average_position_size, 2)
        }

    @staticmethod
    def evaluate_insider_score(profile: Dict[str, Any]) -> float:
        """
        Calculates a final Insider Score bounded strictly between 0.0 and 1.0.
        
        Enforces a minimum sample constraint to protect against false positives
        from pure variance or lucky short-term retail accounts.
        """
        # Defensive Guardrail: Require a meaningful baseline pool of long-shot data.
        # Retail accounts hitting 2 out of 2 extreme long shots look anomalies in rates (100%),
        # but lack statistical stability.
        if profile["long_shot_attempts"] < 5:
            return 0.0

        score = 0.0

        # 1. Long-Shot Precision Deviation (Weight: 40%)
        # Pure statistical probability dictates sub-20% lines succeed <= 20% of the time over variance.
        # Excess performance above this baseline reflects strong structural information edge.
        if profile["long_shot_win_rate"] > 0.20:
            excess_win_pct = profile["long_shot_win_rate"] - 0.20
            score += (excess_win_pct / 0.80) * 0.40

        # 2. Domain Topic Monopolization / Concentration (Weight: 30%)
        # Normal retail accounts spread trades widely across entertainment, politics, macro trends, etc.
        # Insiders typically manifest as single-focus actors specialized in a single silo.
        score += profile["category_concentration"] * 0.30

        # 3. Aggressive New Entrants (Weight: 30%)
        # Accounts appearing shortly before major event resolutions and executing outsized positions
        # receive heavy risk-weighting penalties.
        if profile["account_age_days"] <= 30.0:
            age_scalar = (30.0 - profile["account_age_days"]) / 30.0
            # Scale capital intensity up to a normalized $10k size cap
            position_size_scalar = min(1.0, profile["average_position_size"] / 10000.0)
            score += (age_scalar * position_size_scalar) * 0.30

        return max(0.0, min(1.0, round(score, 4)))