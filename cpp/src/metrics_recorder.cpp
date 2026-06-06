#include "metrics_recorder.h"

namespace polymarket {

    void MetricsRecorder::record_trade_received() noexcept {
        trades_received_.fetch_add(1, std::memory_order_relaxed);
    }

    void MetricsRecorder::record_trade_processed() noexcept {
        trades_processed_.fetch_add(1, std::memory_order_relaxed);
    }

    void MetricsRecorder::record_filter_match() noexcept {
        filter_matches_.fetch_add(1, std::memory_order_relaxed);
    }

    void MetricsRecorder::record_parse_error() noexcept {
        parse_errors_.fetch_add(1, std::memory_order_relaxed);
    }

    void MetricsRecorder::record_push_fail() noexcept {
        push_fail_total_.fetch_add(1, std::memory_order_relaxed);
    }

    void MetricsRecorder::record_pop_empty() noexcept {
        pop_empty_total_.fetch_add(1, std::memory_order_relaxed);
    }

    void MetricsRecorder::observe_latency_us(std::uint64_t latency_us) noexcept {
        std::size_t bucket;
        if (latency_us <= 1) [[likely]] {
            bucket = 0; // Standard fast path for your C++ layer
        } else if (latency_us <= 10) {
            bucket = 1;
        } else if (latency_us <= 100) {
            bucket = 2;
        } else if (latency_us <= 1000) {
            bucket = 3;
        } else [[unlikely]] {
            bucket = 4; // Outlier or network delay
        }
        
        latency_buckets_[bucket].value.fetch_add(1, std::memory_order_relaxed);
    }

    MetricsSnapshot MetricsRecorder::get_snapshot(
        std::size_t normal_depth,
        std::size_t priority_depth,
        std::size_t pool_available
    ) const noexcept {
        
        // Linear atomic load sequences - runs completely lock-free without affecting your hot path threads
        return MetricsSnapshot{
            trades_received_.load(std::memory_order_relaxed),
            trades_processed_.load(std::memory_order_relaxed),
            filter_matches_.load(std::memory_order_relaxed),
            parse_errors_.load(std::memory_order_relaxed),
            push_fail_total_.load(std::memory_order_relaxed),
            pop_empty_total_.load(std::memory_order_relaxed),
            normal_depth,
            priority_depth,
            pool_available,
            latency_buckets_[0].value.load(std::memory_order_relaxed),
            latency_buckets_[1].value.load(std::memory_order_relaxed),
            latency_buckets_[2].value.load(std::memory_order_relaxed),
            latency_buckets_[3].value.load(std::memory_order_relaxed),
            latency_buckets_[4].value.load(std::memory_order_relaxed)
        };
    }

}