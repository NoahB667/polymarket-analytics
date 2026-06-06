#pragma once

#include <cstdint>
#include <atomic>
#include <cstddef>

namespace polymarket {
    // Plain Old Data (POD) structure optimized for clean register layout passing across Python bindings
    struct MetricsSnapshot {
        std::uint64_t trades_received;
        std::uint64_t trades_processed;
        std::uint64_t filter_matches;
        std::uint64_t parse_errors;
        std::uint64_t push_fail_total;
        std::uint64_t pop_empty_total;
        std::size_t normal_depth;
        std::size_t priority_depth;
        std::size_t pool_available;
        std::uint64_t latency_bucket_0_1us;
        std::uint64_t latency_bucket_1_10us;
        std::uint64_t latency_bucket_10_100us;
        std::uint64_t latency_bucket_100_1000us;
        std::uint64_t latency_bucket_1000us_plus;
    };

    class MetricsRecorder {
    public:
        MetricsRecorder() = default;
        ~MetricsRecorder() = default;

        // Deleted copy mechanics ensuring metric singletons can never be duplicated or moved
        MetricsRecorder(const MetricsRecorder&) = delete;
        MetricsRecorder& operator=(const MetricsRecorder&) = delete;

        void record_trade_received() noexcept;
        void record_trade_processed() noexcept;
        void record_filter_match() noexcept;
        void record_parse_error() noexcept;
        void record_push_fail() noexcept;
        void record_pop_empty() noexcept;
        
        void observe_latency_us(std::uint64_t latency_us) noexcept;

        MetricsSnapshot get_snapshot(
            std::size_t normal_depth,
            std::size_t priority_depth,
            std::size_t pool_available
        ) const noexcept;

    private:
        // Cache-aligned wrapper preventing false sharing inside arrays
        struct alignas(64) PaddedCounter {
            std::atomic<std::uint64_t> value{0};
        };

        // Every metric is isolated to its own 64-byte hardware boundary cache line
        alignas(64) std::atomic<std::uint64_t> trades_received_{0};
        alignas(64) std::atomic<std::uint64_t> trades_processed_{0};
        alignas(64) std::atomic<std::uint64_t> filter_matches_{0};
        alignas(64) std::atomic<std::uint64_t> parse_errors_{0};
        alignas(64) std::atomic<std::uint64_t> push_fail_total_{0};
        alignas(64) std::atomic<std::uint64_t> pop_empty_total_{0};

        // Pre-allocated array of cache-line isolated metric buckets
        PaddedCounter latency_buckets_[5];
    };

}