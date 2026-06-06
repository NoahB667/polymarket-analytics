cmake_minimum_required(VERSION 3.16)
project(polymarket_core LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 17)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

include(FetchContent)

FetchContent_Declare(
    nlohmann_json
    GIT_REPOSITORY https://github.com/nlohmann/json.git
    GIT_TAG v3.11.3
)

FetchContent_Declare(
    pybind11 
    GIT_REPOSITORY https://github.com/pybind/pybind11.git
    GIT_TAG v2.12.0
)

FetchContent_MakeAvailable(nlohmann_json pybind11)

find_package(Boost REQUIRED)

add_library(polymarket_core_headers INTERFACE)
target_include_directories(polymarket_core_headers INTERFACE
    ${CMAKE_CURRENT_SOURCE_DIR}/include
)

enable_testing()

add_executable(test_types tests/test_types.cpp)
target_link_libraries(test_types PRIVATE polymarket_core_headers)
add_test(NAME test_types COMMAND test_types)

add_executable(test_json_parser tests/test_json_parser.cpp src/json_parser.cpp)
target_link_libraries(test_json_parser PRIVATE 
    polymarket_core_headers 
    nlohmann_json::nlohmann_json
    Boost::boost
)
add_test(NAME test_json_parser COMMAND test_json_parser)

add_executable(test_trade_filter tests/test_trade_filter.cpp src/trade_filter.cpp)
target_link_libraries(test_trade_filter PRIVATE polymarket_core_headers)
add_test(NAME test_trade_filter COMMAND test_trade_filter)

add_executable(test_memory_pool tests/test_memory_pool.cpp src/memory_pool.cpp)
target_link_libraries(test_memory_pool PRIVATE polymarket_core_headers)
add_test(NAME test_memory_pool COMMAND test_memory_pool)

add_executable(test_trade_queue tests/test_trade_queue.cpp)
target_link_libraries(test_trade_queue PRIVATE Boost::boost polymarket_core_headers)
add_test(NAME test_trade_queue COMMAND test_trade_queue)

pybind11_add_module(polymarket_core
    src/bindings.cpp
    src/core_engine.cpp
    src/json_parser.cpp
    src/trade_filter.cpp
    src/memory_pool.cpp
    src/metrics_recorder.cpp
)
target_include_directories(polymarket_core PRIVATE ${CMAKE_CURRENT_SOURCE_DIR}/include)
target_link_libraries(polymarket_core PRIVATE Boost::boost nlohmann_json::nlohmann_json)