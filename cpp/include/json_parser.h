#pragma once

#include "types.h"
#include <string>
#include <unordered_map>

namespace polymarket {

class TradeSaxEventHandler;

class JsonTradeConverter {
public:
    JsonTradeConverter() = default;
    ~JsonTradeConverter() = default;
    // Pass by string_view to guarantee zero-copy string references from network frames
    bool parse_trade(std::string_view json, RawTrade& out_trade);
private:
    std::unordered_map<std::uint32_t, std::uint32_t> market_hash_cache_;
    std::uint32_t get_market_id(std::string_view market);
    // GRANT FRIENDSHIP: Gives the handler permission to read our private cache map
    friend class TradeSaxEventHandler;
};

}
