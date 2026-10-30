// Shared helpers: HRESULT -> exception, UTF-8 conversion, COM/MF initialisation, QPC clock.
#pragma once

#ifndef NOMINMAX
#define NOMINMAX
#endif

// <unknwn.h> must precede <winrt/base.h> so C++/WinRT can use classic COM interfaces
// (IDirect3DDxgiInterfaceAccess, IMFAsyncCallback, ...) with as<>() and implements<>.
#include <unknwn.h>
#include <inspectable.h>
#include <windows.h>
#include <cguid.h>
#include <winrt/base.h>

#include <cstdint>
#include <cstdio>
#include <stdexcept>
#include <string>

namespace sr {

using winrt::com_ptr;

class HrError : public std::runtime_error {
 public:
  HrError(HRESULT hr, const std::string& message) : std::runtime_error(message), hr_(hr) {}
  HRESULT hr() const noexcept { return hr_; }

 private:
  HRESULT hr_;
};

inline std::string utf8(const std::wstring& w) {
  if (w.empty()) return {};
  int n = WideCharToMultiByte(CP_UTF8, 0, w.data(), static_cast<int>(w.size()), nullptr, 0, nullptr, nullptr);
  std::string s(static_cast<size_t>(n), '\0');
  WideCharToMultiByte(CP_UTF8, 0, w.data(), static_cast<int>(w.size()), s.data(), n, nullptr, nullptr);
  return s;
}

inline std::string hr_to_string(HRESULT hr) {
  wchar_t* buffer = nullptr;
  DWORD len = FormatMessageW(
      FORMAT_MESSAGE_ALLOCATE_BUFFER | FORMAT_MESSAGE_FROM_SYSTEM | FORMAT_MESSAGE_IGNORE_INSERTS, nullptr,
      static_cast<DWORD>(hr), 0, reinterpret_cast<LPWSTR>(&buffer), 0, nullptr);
  std::wstring text;
  if (len != 0 && buffer != nullptr) {
    text.assign(buffer, len);
    LocalFree(buffer);
  }
  while (!text.empty() && (text.back() == L'\r' || text.back() == L'\n' || text.back() == L' ' || text.back() == L'.')) {
    text.pop_back();
  }
  char code[16];
  std::snprintf(code, sizeof(code), "0x%08X", static_cast<unsigned>(hr));
  return text.empty() ? std::string(code) : std::string(code) + " (" + utf8(text) + ")";
}

[[noreturn]] inline void throw_hr(HRESULT hr, const char* what) {
  throw HrError(hr, std::string(what) + " failed: " + hr_to_string(hr));
}

inline void check(HRESULT hr, const char* what) {
  if (FAILED(hr)) throw_hr(hr, what);
}

inline bool is_device_lost(HRESULT hr) {
  return hr == DXGI_ERROR_DEVICE_REMOVED || hr == DXGI_ERROR_DEVICE_RESET || hr == DXGI_ERROR_DEVICE_HUNG ||
         hr == DXGI_ERROR_DRIVER_INTERNAL_ERROR;
}

// Joins the multithreaded apartment on the calling thread once. If the thread already
// belongs to an STA (Qt's GUI thread calls OleInitialize) the call is a harmless no-op.
inline void ensure_com() {
  struct ComInit {
    HRESULT hr = CoInitializeEx(nullptr, COINIT_MULTITHREADED);
    ~ComInit() {
      if (SUCCEEDED(hr)) CoUninitialize();
    }
  };
  thread_local ComInit init;
}

// Media Foundation is started once per process and kept alive until exit.
void ensure_mf();

// QueryPerformanceCounter in 100 ns units (the Media Foundation time base).
inline int64_t qpc_now_hns() {
  static const int64_t freq = [] {
    LARGE_INTEGER f;
    QueryPerformanceFrequency(&f);
    return f.QuadPart;
  }();
  LARGE_INTEGER c;
  QueryPerformanceCounter(&c);
  return (c.QuadPart / freq) * 10'000'000 + (c.QuadPart % freq) * 10'000'000 / freq;
}

// Exact constant-frame-rate timestamp for frame `index`; strictly increasing in `index`.
inline int64_t frame_time_hns(int64_t index, uint32_t fps) {
  return index * 10'000'000 / static_cast<int64_t>(fps);
}

}  // namespace sr
