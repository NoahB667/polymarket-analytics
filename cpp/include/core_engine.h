#pragma once

#include "types.h"
#include "memory_pool.h"
#include "json_parser.h"
#include "trade_queue.h"
#include "trade_filter.h"
#include "metrics_recorder.h"
#include <string_view>

namespace polymarket {

class CoreEngine {
public:
    CoreEngine(std::size_t pool_capacity, std::size_t queue_capacity);
    ~CoreEngine() = default;
    bool process_json(std::string_view json);
    bool pop_priority(RawTrade& out_trade) noexcept;
    bool pop_normal(RawTrade& out_trade) noexcept;
    std::unique_ptr<RawTrade> pop_priority() noexcept;
    std::unique_ptr<RawTrade> pop_normal() noexcept;
    MetricsSnapshot get_stats() const noexcept;
    void update_subscription(std::uint32_t chat_id, std::uint64_t market_hash, double min_usd);
    void remove_subscription(std::uint32_t chat_id, std::uint64_t market_hash);

private:
    ObjectPool pool_;
    JsonTradeConverter parser_;
    TradeQueue priority_queue_;
    TradeQueue normal_queue_;
    TradeFilter filter_;
    MetricsRecorder metrics_;
};

}