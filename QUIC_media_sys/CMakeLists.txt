cmake_minimum_required(VERSION 3.16)
project(QUIC_media_sys LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED True)
set(CMAKE_POSITION_INDEPENDENT_CODE ON)

add_compile_definitions(QUIC_API_ENABLE_PREVIEW_FEATURES=1)


# =============================
# MsQuic paths
# =============================
set(MSQUIC_ROOT "$ENV{HOME}/msquic")
set(MSQUIC_LIB "${MSQUIC_ROOT}/build/bin/Debug")
set(MSQUIC_INC "${MSQUIC_ROOT}/src/inc")

# =============================
# Global include (整个工程可见)
# =============================
include_directories(
    ${CMAKE_SOURCE_DIR}/include
    ${CMAKE_SOURCE_DIR}/src
)

# =============================
# Application Layer
# =============================
add_library(moq_app
    src/app/packetizer.cpp
    src/app/reassembler.cpp
    src/app/scheduler.cpp
    src/app/sinks.cpp
    src/app/h265_parser.cpp
    src/app/av1_parser.cpp
)

# =============================
# Transport Layer
# =============================
add_library(moq_transport
    src/transport/msquic_transport.cpp
)

# MsQuic include 只给 transport
target_include_directories(moq_transport PUBLIC
    ${MSQUIC_INC}
)

# MsQuic lib 目录
target_link_libraries(moq_transport
    ${MSQUIC_LIB}/libmsquic.so
)

# =============================
# Client
# =============================
#add_executable(moq_client
#    src/main/moq_client.cpp
#)

#target_link_libraries(moq_client
#    moq_app
#    moq_transport
#)

# =============================
# Server
# =============================
#add_executable(moq_server
#    src/main/moq_server.cpp
#)

#target_link_libraries(moq_server
#    moq_app
#    moq_transport
#)

# =============================
# Python Module (Pybind11)
# =============================
include(FetchContent)
FetchContent_Declare(
  pybind11
  GIT_REPOSITORY https://github.com/pybind/pybind11.git
  GIT_TAG        v2.11.1
)
FetchContent_MakeAvailable(pybind11)

pybind11_add_module(quic_media
    python/quic_media_py.cpp
    src/api/quic_media_client.cpp
    src/api/quic_media_server.cpp
)
target_include_directories(quic_media PUBLIC
    ${CMAKE_SOURCE_DIR}/include
    ${CMAKE_SOURCE_DIR}/src
)
target_link_libraries(quic_media PRIVATE
    moq_app
    moq_transport
)

# Post-build copy to python directory
add_custom_command(TARGET quic_media POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E make_directory ${CMAKE_SOURCE_DIR}/python/lib
    COMMAND ${CMAKE_COMMAND} -E copy $<TARGET_FILE:quic_media> ${CMAKE_SOURCE_DIR}/python/lib/
    COMMAND ${CMAKE_COMMAND} -E copy ${MSQUIC_LIB}/libmsquic.so ${CMAKE_SOURCE_DIR}/python/lib/
)