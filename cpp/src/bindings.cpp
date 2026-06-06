#include <pybind11/pybind11.h>
#include <pybind11/stl.h>
#include "core_engine.h"
#include "types.h"
#include "metrics_recorder.h"

namespace py = pybind11;

PYBIND11_MODULE(polymarket_core, m) {
    m.doc() = "Ultra low-latency HFT ingestion core engine for Polymarket data feeds";

    py::enum_<polymarket::Side>(m, "Side", py::arithmetic())
        .value("Unknown", polymarket::Side::Unknown)
        .value("Buy", polymarket::Side::Buy)
        .value("Sell", polymarket::Side::Sell)
        .export_values();

    py::class_<polymarket::RawTrade>(m, "RawTrade")
        .def(py::init<>()) 
        .def_readonly("market_id", &polymarket::RawTrade::market_id)
        .def_readonly("asset_id", &polymarket::RawTrade::asset_id)
        .def_readonly("price", &polymarket::RawTrade::price)
        .def_readonly("size", &polymarket::RawTrade::size)
        .def_readonly("usd", &polymarket::RawTrade::usd)
        .def_readonly("side", &polymarket::RawTrade::side)
        .def_readonly("timestamp_ms", &polymarket::RawTrade::timestamp_ms);

    py::class_<polymarket::MetricsSnapshot>(m, "MetricsSnapshot")
        .def_readonly("received_total", &polymarket::MetricsSnapshot::trades_received)
        .def_readonly("processed_total", &polymarket::MetricsSnapshot::trades_processed)
        .def_readonly("filter_matches", &polymarket::MetricsSnapshot::filter_matches)
        .def_readonly("parse_errors", &polymarket::MetricsSnapshot::parse_errors)
        .def_readonly("push_fail_total", &polymarket::MetricsSnapshot::push_fail_total)
        .def_readonly("pop_empty_total", &polymarket::MetricsSnapshot::pop_empty_total)
        .def_readonly("normal_depth", &polymarket::MetricsSnapshot::normal_depth)
        .def_readonly("priority_depth", &polymarket::MetricsSnapshot::priority_depth)
        .def_readonly("pool_available", &polymarket::MetricsSnapshot::pool_available)
        .def_readonly("latency_bucket_0_1us", &polymarket::MetricsSnapshot::latency_bucket_0_1us)
        .def_readonly("latency_bucket_1_10us", &polymarket::MetricsSnapshot::latency_bucket_1_10us)
        .def_readonly("latency_bucket_10_100us", &polymarket::MetricsSnapshot::latency_bucket_10_100us)
        .def_readonly("latency_bucket_100_1000us", &polymarket::MetricsSnapshot::latency_bucket_100_1000us)
        .def_readonly("latency_bucket_1000us_plus", &polymarket::MetricsSnapshot::latency_bucket_1000us_plus);

    py::class_<polymarket::CoreEngine>(m, "CoreEngine")
        .def(py::init<std::size_t, std::size_t>(), py::arg("pool_capacity"), py::arg("queue_capacity"))
        .def("process_json", &polymarket::CoreEngine::process_json, py::arg("json"), 
             "Processes a raw incoming WebSocket data frame text.")
        .def("pop_priority", &polymarket::CoreEngine::pop_priority, py::arg("out_trade"),
             "Pulls high-suspicion anomalies directly into your pre-allocated Python trade target reference.")
        .def("pop_normal", &polymarket::CoreEngine::pop_normal, py::arg("out_trade"),
             "Pulls standard trades directly into your pre-allocated Python trade target reference.")
        .def("get_stats", &polymarket::CoreEngine::get_stats, "Collects a lock-free snapshot of engine metrics.")
        .def("update_subscription", &polymarket::CoreEngine::update_subscription, py::arg("market_id"), py::arg("user_id"), py::arg("min_usd"))
        .def("remove_subscription", &polymarket::CoreEngine::remove_subscription, py::arg("market_id"), py::arg("user_id"));
}