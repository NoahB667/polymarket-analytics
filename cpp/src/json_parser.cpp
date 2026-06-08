#include "json_parser.h"
#include <string_view>
#include <charconv> 
#include <type_traits>

namespace {
    constexpr std::uint32_t hash32(std::string_view value) noexcept {
        std::uint32_t hash = 2166136261u;
        for (char c : value) {
            hash ^= static_cast<std::uint8_t>(c);
            hash *= 16777619u;
        }
        return hash;
    }

    constexpr std::uint64_t hash64(std::string_view value) noexcept {
        std::uint64_t hash = 1469598103934665603ull;
        for (char c : value) {
            hash ^= static_cast<std::uint8_t>(c);
            hash *= 1099511628211ull;
        }
        return hash;
    }

    // Locale-independent parsing for integer segments
    template <typename T>
    inline typename std::enable_if<std::is_integral<T>::value, bool>::type
    fast_numeric_parse(std::string_view sv, T& out_value) noexcept {
        if (sv.empty()) return false;
        auto [ptr, ec] = std::from_chars(sv.data(), sv.data() + sv.size(), out_value);
        return ec == std::errc{};
    }

    // Locale-independent parsing for floating-point segments
    template <typename T>
    inline typename std::enable_if<std::is_floating_point<T>::value, bool>::type
    fast_numeric_parse(std::string_view sv, T& out_value) noexcept {
        if (sv.empty()) return false;

        double result = 0.0;
        double sign = 1.0;
        std::size_t i = 0;

        if (sv[0] == '-') {
            sign = -1.0;
            i++;
        } else if (sv[0] == '+') {
            i++;
        }

        bool has_digits = false;
        while (i < sv.size() && sv[i] >= '0' && sv[i] <= '9') {
            result = result * 10.0 + (sv[i] - '0');
            i++;
            has_digits = true;
        }

        if (i < sv.size() && sv[i] == '.') {
            i++;
            double factor = 0.1;
            while (i < sv.size() && sv[i] >= '0' && sv[i] <= '9') {
                result += (sv[i] - '0') * factor;
                factor *= 0.1;
                i++;
                has_digits = true;
            }
        }

        if (!has_digits) return false;

        out_value = static_cast<T>(sign * result);
        return true;
    }

    // Direct memory extraction helper to grab the contents inside quotes
    inline std::string_view extract_field_value(std::string_view json, std::string_view key) noexcept {
        std::size_t pos = json.find(key);
        if (pos == std::string_view::npos) return {};

        // Find the start of the value payload string after the colon marker
        std::size_t start_pos = json.find(':', pos + key.size());
        if (start_pos == std::string_view::npos) return {};

        std::size_t open_quote = json.find('"', start_pos);
        std::size_t close_quote = json.find('"', open_quote + 1);

        if (open_quote == std::string_view::npos || close_quote == std::string_view::npos) {
            // Fallback for unquoted numeric fields if they occur
            std::size_t first_digit = json.find_first_not_of(" \t\n\r:", start_pos);
            std::size_t end_digit = json.find_first_of(", \t\n\r}", first_digit);
            if (first_digit != std::string_view::npos) {
                return json.substr(first_digit, (end_digit == std::string_view::npos) ? std::string_view::npos : (end_digit - first_digit));
            }
            return {};
        }

        return json.substr(open_quote + 1, close_quote - open_quote - 1);
    }
}

namespace polymarket {

    ParseResult JsonTradeConverter::parse_trade(std::string_view json, RawTrade& out_trade) {
        // 1. Verify event type immediately to discard invalid frame profiles
        std::string_view event_type = extract_field_value(json, "\"event_type\"");

        if (event_type.empty()) return ParseResult::InvalidFormat;

        if (event_type != "last_trade_price") return ParseResult::Skip;

        // 2. Extract string-wrapped text data segments
        std::string_view market_sv = extract_field_value(json, "\"market\"");
        std::string_view asset_sv = extract_field_value(json, "\"asset_id\"");
        std::string_view price_sv = extract_field_value(json, "\"price\"");
        std::string_view size_sv = extract_field_value(json, "\"size\"");
        std::string_view side_sv = extract_field_value(json, "\"side\"");
        std::string_view time_sv = extract_field_value(json, "\"timestamp\"");

        if (market_sv.empty() || asset_sv.empty() || price_sv.empty() || size_sv.empty()) {
            return ParseResult::InvalidFormat;
        }

        // 3. Assign and parse identity states
        out_trade.market_id = get_market_id(market_sv);
        out_trade.asset_id = hash64(asset_sv);

        if (side_sv == "BUY") out_trade.side = Side::Buy;
        else if (side_sv == "SELL") out_trade.side = Side::Sell;
        else out_trade.side = Side::Unknown;

        // 4. Parse values into their target primitives
        if (!fast_numeric_parse(price_sv, out_trade.price)) return ParseResult::InvalidFormat;
        if (!fast_numeric_parse(size_sv, out_trade.size)) return ParseResult::InvalidFormat;
        if (!fast_numeric_parse(time_sv, out_trade.timestamp_ms)) return ParseResult::InvalidFormat;

        // 5. Compute derived matrix results
        out_trade.usd = out_trade.price * out_trade.size;
        return ParseResult::Success;
    }

    std::uint32_t JsonTradeConverter::get_market_id(std::string_view market) {
        std::uint32_t m_hash = hash32(market);
        auto entry = market_hash_cache_.find(m_hash);
        if (entry != market_hash_cache_.end()) {
            return entry->second;
        }
        market_hash_cache_.emplace(m_hash, m_hash);
        return m_hash;
    }
}