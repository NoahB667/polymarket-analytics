#include "json_parser.h"
#include <cassert>
#include <string>
#include <cmath>
#include <iostream>

namespace polymarket {
    AnomalyScore score_trade(const RawTrade& trade) noexcept {
        AnomalyScore s;
        if (trade.usd > 50.0) {
            s.score = 1; 
        } else {
            s.score = 0;
        }
        return s;
    }
}

int main() {
    const std::string msg = "{\"asset_id\":\"114122071509644379678018727908709560226618148003371446110114509806601493071694\",\"event_type\":\"last_trade_price\",\"fee_rate_bps\":\"0\",\"market\":\"0x6a67b9d828d53862160e470329ffea5246f338ecfffdf2cab45211ec578b0347\",\"price\":\"0.456\",\"side\":\"BUY\",\"size\":\"219.217767\",\"timestamp\":\"1750428146322\"}";

    polymarket::RawTrade trade{};
    polymarket::JsonTradeConverter parser;
    
    bool ok = parser.parse_trade(msg, trade);
    if (!ok) {
        std::cerr << "Parsing failed internally inside SAX engine loop." << std::endl;
        // Fail explicitly with an error code to pinpoint tracking
        return 1;
    }

    // Integrity validations
    assert(trade.market_id != 0);
    assert(trade.asset_id != 0);
    assert(trade.side == polymarket::Side::Buy);
    assert(trade.timestamp_ms == 1750428146322); 

    constexpr double epsilon = 1e-9;
    assert(std::abs(trade.price - 0.456) < epsilon);
    assert(std::abs(trade.size - 219.217767) < epsilon);
    
    double expected_usd = 0.456 * 219.217767;
    assert(std::abs(trade.usd - expected_usd) < epsilon);

    std::cout << "All parser unit validations passed cleanly!" << std::endl;
    return 0;
}