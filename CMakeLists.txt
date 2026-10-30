cmake_minimum_required(VERSION 3.20)
project(screenrecorder_native LANGUAGES CXX)

if(NOT WIN32)
  message(FATAL_ERROR "The _srnative extension targets Windows 10/11 (Direct3D 11, WGC, Media Foundation).")
endif()

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Python 3.12 REQUIRED COMPONENTS Interpreter Development.Module)
if(NOT pybind11_DIR)
  execute_process(
    COMMAND "${Python_EXECUTABLE}" -m pybind11 --cmakedir
    OUTPUT_VARIABLE pybind11_DIR
    OUTPUT_STRIP_TRAILING_WHITESPACE)
endif()
find_package(pybind11 CONFIG REQUIRED)

pybind11_add_module(_srnative
  native/module.cpp
  native/d3d_device.cpp
  native/frame_converter.cpp
  native/mf_encoder.cpp
  native/mf_caps.cpp
  native/wgc_capture.cpp)

# C++/WinRT headers ship with the Windows SDK under Include/<version>/cppwinrt. MSBuild adds
# that folder automatically; locate it explicitly so Ninja/other generators work as well.
set(_sdk_version "${CMAKE_VS_WINDOWS_TARGET_PLATFORM_VERSION}")
if(NOT _sdk_version)
  set(_sdk_version "${CMAKE_SYSTEM_VERSION}")
endif()
find_path(CPPWINRT_INCLUDE_DIR winrt/base.h
  HINTS
    "$ENV{WindowsSdkDir}Include/${_sdk_version}/cppwinrt"
    "C:/Program Files (x86)/Windows Kits/10/Include/${_sdk_version}/cppwinrt")
if(CPPWINRT_INCLUDE_DIR)
  target_include_directories(_srnative PRIVATE "${CPPWINRT_INCLUDE_DIR}")
endif()

target_compile_definitions(_srnative PRIVATE UNICODE _UNICODE NOMINMAX _WIN32_WINNT=0x0A00 WINVER=0x0A00)
target_compile_options(_srnative PRIVATE /W3 /permissive- /EHsc /bigobj /utf-8 /Zc:__cplusplus)
target_link_libraries(_srnative PRIVATE
  d3d11 dxgi d3dcompiler
  mfplat mfreadwrite mfuuid mf evr
  windowsapp ole32 oleaut32)

if(SKBUILD)
  install(TARGETS _srnative DESTINATION screenrecorder)
else()
  # Plain CMake build: drop the .pyd next to the Python sources so `python -m screenrecorder`,
  # pytest and PyInstaller pick it up from the source tree.
  add_custom_command(TARGET _srnative POST_BUILD
    COMMAND ${CMAKE_COMMAND} -E copy "$<TARGET_FILE:_srnative>" "${CMAKE_SOURCE_DIR}/screenrecorder/")
endif()
