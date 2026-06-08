#pragma once

#include <cstdint>

namespace polymarket {

    enum class Side : std::uint8_t{
        Unknown = 0,
        Buy = 1,
        Sell = 2
    };

    // exactly 56 bytes
    struct alignas(8) RawTrade {
        std::uint64_t asset_id{0};
        std::int64_t timestamp_ms{0};
        std::uint64_t ingestion_time_us{0};
        double price{0.0};
        double size{0.0};
        double usd{0.0};
        std::uint32_t market_id{0};
        Side side{Side::Unknown};
        std::uint8_t reserved[3]{0,0,0}; // explicit padding
    };

    // 4 byte
    struct AnomalyScore {
        std::uint8_t score{0};
        bool is_large_trade{false};
        bool is_long_shot{false};
        bool is_volume_spike{false};
    };

}
