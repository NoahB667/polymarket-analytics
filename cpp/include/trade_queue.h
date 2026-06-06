#pragma once

#include "types.h"
#include <boost/lockfree/spsc_queue.hpp>
#include <cstddef>

namespace polymarket {

// Align the entire class to a 64-byte boundary to isolate it on its own CPU Cache Lines
class alignas(64) TradeQueue {
public:
    // Boost lock-free queues require the capacity to be known at compile time for maximum 
    // ring-buffer optimization, or passed via constructor for runtime initialization.
    TradeQueue() = default;
    ~TradeQueue() = default;

    // Disallow copies and moves to protect ring-buffer memory stability
    TradeQueue(const TradeQueue&) = delete;
    TradeQueue& operator=(const TradeQueue&) = delete;

    /**
     * @brief High-performance pass-by-value push.
     * Copies the flat 48-byte struct directly into the ring buffer slot.
     */
    inline bool try_push(const RawTrade& trade) noexcept {
        return queue_.push(trade);
    }

    /**
     * @brief High-performance pass-by-value pop.
     * Extracts the trade directly into Python-accessible memory.
     */
    inline bool try_pop(RawTrade& out_trade) noexcept {
        return queue_.pop(out_trade);
    }

    /**
     * @brief Returns the exact number of unconsumed elements remaining in the queue.
     */
    inline std::size_t size() const noexcept {
        return queue_.read_available();
    }

    /**
     * @brief True if the consumer has completely drained the pipeline.
     */
    inline bool empty() noexcept {
        return queue_.empty();
    }

private:
    // Force the internal ring buffer storage structure to separate its tracking pointers
    alignas(64) boost::lockfree::spsc_queue<RawTrade, boost::lockfree::capacity<131072>> queue_; 
    // Note: If you want runtime capacity, remove the boost::lockfree::capacity template parameter,
    // but setting a fixed power-of-two compile-time size lets the compiler replace slow division/modulo 
    // indexing operators with lightning-fast bitwise AND operations.
};

}