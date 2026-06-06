#pragma once

#include "types.h"
#include <cstdint>
#include <vector>
#include <unordered_map>
#include <shared_mutex>

namespace polymarket {

    // Global, inline-optimized anomaly pre-filter
    inline AnomalyScore score_trade(const RawTrade& trade) noexcept {
        bool is_large_trade = (trade.usd >= 10000.0);
        bool is_long_shot   = (trade.price > 0.0 && trade.price <= 0.20);
        
        // Volume spike checking will be calculated in Step 5 using Redis data
        std::uint8_t total_score = static_cast<std::uint8_t>(is_large_trade) + static_cast<std::uint8_t>(is_long_shot);

        return AnomalyScore{
            total_score,
            is_large_trade,
            is_long_shot,
            false // Volume spike placeholder
        };
    }

    class TradeFilter {
    public:
        struct Subscription {
            std::uint64_t user_id;
            double min_usd;
        };

        TradeFilter() = default;
        ~TradeFilter() = default;

        // Configuration methods called via the Python control channel
        void update_subscription(std::uint32_t market_id, std::uint64_t user_id, double min_usd);
        void remove_subscription(std::uint32_t market_id, std::uint64_t user_id);
        
        /**
         * @brief Zero-allocation routing check.
         * Writes matching IDs directly into a pre-allocated stack buffer array.
         * @return Number of valid matches found and written.
         */
        std::size_t match_subscribers(const RawTrade& trade, std::uint64_t* out_matches, std::size_t max_matches) const noexcept;

    private:
        // Contiguous memory blocks for each market to maximize CPU cache prefetching efficiency
        std::unordered_map<std::uint32_t, std::vector<Subscription>> market_routes_;
        
        // Mutable shared mutex protecting subscription modifications
        mutable std::shared_mutex filter_mutex_;
    };

}