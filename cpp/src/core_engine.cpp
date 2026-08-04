#include "core_engine.h"
#include <string_view>

namespace polymarket {

    // Keep pool_ in the initializer list if other components need it, 
    // but we remove its usage from our parsing hot-path.
    CoreEngine::CoreEngine(
        std::size_t pool_capacity,
        std::size_t queue_capacity,
        double long_shot_price_threshold,
        double large_trade_usd_threshold
    )
        : pool_(pool_capacity),
          priority_queue_(),
          normal_queue_(),
          filter_(),
          metrics_(),
          long_shot_price_threshold_(long_shot_price_threshold),
          large_trade_usd_threshold_(large_trade_usd_threshold) {}

    bool CoreEngine::process_json(std::string_view json) {
        if (json == "ping" || json == "\"ping\"" || json.empty()) [[unlikely]] {
            return true; 
        }

        // Capture precise entry time before running the parser
        auto entry_time = std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now().time_since_epoch()
        ).count();

        metrics_.record_trade_received();

        // ZERO-COPY OVERHEAD: Parse directly into a clean 56-byte stack variable
        RawTrade trade{}; 
        trade.ingestion_time_us = static_cast<std::uint64_t>(entry_time);

        ParseResult result = parser_.parse_trade(json, trade);

        if (result == ParseResult::Skip) [[likely]] {
            // Drop non-trade messages silently without incrementing parse_errors
            return true; 
        }
        
        if (result == ParseResult::InvalidFormat) [[unlikely]] {
            metrics_.record_parse_error(); // True semantic/formatting failure
            return false;
        }

        // Run anomaly detection flags
        AnomalyScore flags = score_trade(trade, long_shot_price_threshold_, large_trade_usd_threshold_);

        bool pushed = false;
        if (flags.score > 0) {
            // Pushes a copy of the stack object straight into the pre-allocated ring buffer slot
            pushed = priority_queue_.try_push(trade);
        } else {
            pushed = normal_queue_.try_push(trade);
        }

        if (pushed) [[likely]] {
            metrics_.record_trade_processed();
        } else {
            metrics_.record_push_fail();
            return false;
        }

        return true;
    }

    bool CoreEngine::pop_priority(RawTrade& out_trade) noexcept {
        if (!priority_queue_.try_pop(out_trade)) {
            metrics_.record_pop_empty();
            return false;
        }
        
        auto exit_time = std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now().time_since_epoch()
        ).count();

        std::uint64_t now = static_cast<std::uint64_t>(exit_time);
        
        if (now > out_trade.ingestion_time_us) {
            // Measures pure internal processing speed in microseconds
            metrics_.observe_latency_us(now - out_trade.ingestion_time_us);
        }

        return true;
    }

    bool CoreEngine::pop_normal(RawTrade& out_trade) noexcept {
        if (!normal_queue_.try_pop(out_trade)) {
            metrics_.record_pop_empty();
            return false;
        }

        auto exit_time = std::chrono::duration_cast<std::chrono::microseconds>(
            std::chrono::steady_clock::now().time_since_epoch()
        ).count();

        std::uint64_t now = static_cast<std::uint64_t>(exit_time);
        
        if (now > out_trade.ingestion_time_us) {
            metrics_.observe_latency_us(now - out_trade.ingestion_time_us);
        }

        return true;
    }

    std::unique_ptr<RawTrade> CoreEngine::pop_priority() noexcept {
        auto trade = std::make_unique<RawTrade>();
        if (this->pop_priority(*trade)) {
            return trade;
        }
        return nullptr; // Returns None to Python cleanly
    }

    std::unique_ptr<RawTrade> CoreEngine::pop_normal() noexcept {
        auto trade = std::make_unique<RawTrade>();
        if (this->pop_normal(*trade)) {
            return trade;
        }
        return nullptr; // Returns None to Python cleanly
    }

    void CoreEngine::update_subscription(std::uint32_t chat_id, std::uint64_t market_hash, double min_usd) {
        // Map Python parameters to the TradeFilter's internal signature types:
        // market_hash maps to market_id (std::uint32_t)
        // chat_id maps to user_id (std::uint64_t)
        std::uint32_t market_id = static_cast<std::uint32_t>(market_hash);
        std::uint64_t user_id = static_cast<std::uint64_t>(chat_id);

        filter_.update_subscription(market_id, user_id, min_usd);
    }

    void CoreEngine::remove_subscription(std::uint32_t chat_id, std::uint64_t market_hash) {
        std::uint32_t market_id = static_cast<std::uint32_t>(market_hash);
        std::uint64_t user_id = static_cast<std::uint64_t>(chat_id);

        filter_.remove_subscription(market_id, user_id);
    }

    MetricsSnapshot CoreEngine::get_stats() const noexcept {
        auto& mutable_pq = const_cast<TradeQueue&>(priority_queue_);
        auto& mutable_nq = const_cast<TradeQueue&>(normal_queue_);

        return metrics_.get_snapshot(
            mutable_nq.size(),
            mutable_pq.size(),
            pool_.available()
        );
    }

}