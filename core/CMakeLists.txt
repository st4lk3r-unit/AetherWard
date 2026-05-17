find_package(PkgConfig)

if(PkgConfig_FOUND)
    pkg_check_modules(PCAP libpcap)
endif()

include(CheckIncludeFile)
check_include_file("sys/timepps.h" HAVE_TIMEPPS_H)

add_library(aw SHARED
    src/capture/aw_capture.c
    src/sync/aw_sync.c
    src/tdoa/aw_tdoa.c
    src/signal/aw_signal.c
)

target_include_directories(aw
    PUBLIC  include
    PRIVATE ${PCAP_INCLUDE_DIRS}
)

target_link_libraries(aw
    PRIVATE $<$<BOOL:${PCAP_FOUND}>:${PCAP_LIBRARIES}> m pthread
)

if(PCAP_FOUND)
    target_compile_definitions(aw PRIVATE AW_HAVE_PCAP=1)
    message(STATUS "libpcap found — native capture enabled")
else()
    message(STATUS "libpcap not found — native capture disabled (Python scapy backend still works)")
endif()

if(HAVE_TIMEPPS_H)
    target_compile_definitions(aw PRIVATE AW_HAVE_PPS=1)
    message(STATUS "sys/timepps.h found — PPS synchronisation enabled")
else()
    message(STATUS "sys/timepps.h not found — PPS sync disabled (software clock fallback active)")
endif()

target_compile_options(aw PRIVATE -Wall -Wextra -O2 -fPIC)

set_target_properties(aw PROPERTIES
    VERSION   ${PROJECT_VERSION}
    SOVERSION 0
    OUTPUT_NAME aw
)

install(TARGETS     aw          LIBRARY DESTINATION lib)
install(DIRECTORY   include/    DESTINATION include/aetherward)
