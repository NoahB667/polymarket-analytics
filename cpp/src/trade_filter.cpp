#include "trade_filter.h"
#include <mutex>
#include <algorithm>

namespace polymarket {

    void TradeFilter::update_subscription(std::uint32_t market_id, std::uint64_t user_id, double min_usd) {
        std::unique_lock<std::shared_mutex> lock(filter_mutex_);
        auto& subscriptions = market_routes_[market_id];
        
        // Scan the contiguous vector for an existing user configuration
        auto it = std::find_if(subscriptions.begin(), subscriptions.end(),
            [user_id](const Subscription& sub) { return sub.user_id == user_id; });

        if (it != subscriptions.end()) {
            it->min_usd = min_usd; // Update existing user threshold
        } else {
            subscriptions.push_back(Subscription{user_id, min_usd}); // Append fresh routing target
        }
    }

    void TradeFilter::remove_subscription(std::uint32_t market_id, std::uint64_t user_id) {
        std::unique_lock<std::shared_mutex> lock(filter_mutex_);
        auto map_it = market_routes_.find(market_id);
        if (map_it == market_routes_.end()) return;

        auto& subscriptions = map_it->second;
        subscriptions.erase(
            std::remove_if(subscriptions.begin(), subscriptions.end(),
                [user_id](const Subscription& sub) { return sub.user_id == user_id; }),
            subscriptions.end()
        );

        if (subscriptions.empty()) {
            market_routes_.erase(map_it);
        }
    }
    
    std::size_t TradeFilter::match_subscribers(const RawTrade& trade, std::uint64_t* out_matches, std::size_t max_matches) const noexcept {
        // High-performance shared read-lock block
        std::shared_lock<std::shared_mutex> lock(filter_mutex_);
        
        auto map_it = market_routes_.find(trade.market_id);
        if (map_it == market_routes_.end()) {
            return 0; // Fast path: No active subscribers found for this market
        }

        std::size_t match_count = 0;
        const auto& subscriptions = map_it->second;
        const std::size_t sub_count = subscriptions.size();

        // Sequential array pass optimized for execution vectorization and cache locality
        for (std::size_t i = 0; i < sub_count; ++i) {
            const auto& sub = subscriptions[i];
            if (trade.usd >= sub.min_usd) {
                out_matches[match_count++] = sub.user_id;
                
                // Safety boundary check preventing stack overflow issues
                if (match_count >= max_matches) [[unlikely]] {
                    break;
                }
            }
        }

        return match_count;
    }

}