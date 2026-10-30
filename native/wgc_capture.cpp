#include "wgc_capture.h"

#include <windows.graphics.capture.interop.h>
#include <windows.graphics.directx.direct3d11.interop.h>
#include <winrt/Windows.Foundation.Metadata.h>

#include <algorithm>
#include <chrono>

namespace sr {

namespace wgc = winrt::Windows::Graphics::Capture;
using winrt::Windows::Graphics::DirectX::DirectXPixelFormat;

bool wgc_is_supported() {
  ensure_com();
  try {
    return wgc::GraphicsCaptureSession::IsSupported();
  } catch (...) {
    return false;
  }
}

// ---------------------------------------------------------------------------------------
// WgcCapture

WgcCapture::WgcCapture(std::shared_ptr<D3DDevice> device, const CaptureTarget& target, bool cursor, bool border,
                       UINT fps)
    : device_(std::move(device)) {
  (void)fps;
  ensure_com();
  // PyWinRT cannot create capture items from HWND/HMONITOR: that needs the classic COM
  // IGraphicsCaptureItemInterop factory, which is why this lives in the native module.
  auto interop = winrt::get_activation_factory<wgc::GraphicsCaptureItem, IGraphicsCaptureItemInterop>();
  const auto& iid = reinterpret_cast<const IID&>(winrt::guid_of<wgc::IGraphicsCaptureItem>());
  if (target.kind == SourceKind::Monitor) {
    check(interop->CreateForMonitor(reinterpret_cast<HMONITOR>(target.handle), iid, winrt::put_abi(item_)),
          "IGraphicsCaptureItemInterop::CreateForMonitor");
  } else if (target.kind == SourceKind::Window) {
    check(interop->CreateForWindow(reinterpret_cast<HWND>(target.handle), iid, winrt::put_abi(item_)),
          "IGraphicsCaptureItemInterop::CreateForWindow");
  } else {
    throw HrError(E_INVALIDARG, "WgcCapture requires a monitor or window target");
  }

  {
    std::lock_guard<std::recursive_mutex> guard(device_->lock());
    winrt_device_ = device_->winrt_device();
  }
  pool_size_ = item_.Size();
  pool_ = wgc::Direct3D11CaptureFramePool::CreateFreeThreaded(winrt_device_, DirectXPixelFormat::B8G8R8A8UIntNormalized,
                                                             2, pool_size_);
  session_ = pool_.CreateCaptureSession(item_);
  try {
    session_.IsCursorCaptureEnabled(cursor);
  } catch (...) {
  }
  using winrt::Windows::Foundation::Metadata::ApiInformation;
  if (ApiInformation::IsPropertyPresent(L"Windows.Graphics.Capture.GraphicsCaptureSession", L"IsBorderRequired")) {
    try {
      session_.IsBorderRequired(border);
    } catch (...) {
      // Older builds or policy may refuse a borderless capture; the yellow border stays.
    }
  }
  closed_revoker_ = item_.Closed(winrt::auto_revoke, [this](auto const&, auto const&) { closed_.store(true); });
  session_.StartCapture();
}

WgcCapture::~WgcCapture() {
  try {
    close();
  } catch (...) {
  }
}

bool WgcCapture::poll() {
  if (!pool_) return false;
  wgc::Direct3D11CaptureFrame latest{nullptr};
  for (;;) {
    auto frame = pool_.TryGetNextFrame();
    if (!frame) break;
    if (latest) latest.Close();
    latest = frame;
  }
  if (!latest) return false;

  const auto content = latest.ContentSize();
  auto access = latest.Surface().as<::Windows::Graphics::DirectX::Direct3D11::IDirect3DDxgiInterfaceAccess>();
  com_ptr<ID3D11Texture2D> frame_texture;
  check(access->GetInterface(IID_PPV_ARGS(frame_texture.put())), "IDirect3DDxgiInterfaceAccess::GetInterface");
  D3D11_TEXTURE2D_DESC desc{};
  frame_texture->GetDesc(&desc);
  const UINT cw = std::min<UINT>(content.Width > 0 ? static_cast<UINT>(content.Width) : desc.Width, desc.Width);
  const UINT ch = std::min<UINT>(content.Height > 0 ? static_cast<UINT>(content.Height) : desc.Height, desc.Height);

  {
    // GPU->GPU copy so the pool buffer is handed back to WGC immediately.
    std::lock_guard<std::recursive_mutex> guard(device_->lock());
    D3D11_TEXTURE2D_DESC own{};
    if (texture_) texture_->GetDesc(&own);
    if (!texture_ || own.Width != desc.Width || own.Height != desc.Height) {
      texture_ = device_->create_texture(desc.Width, desc.Height, DXGI_FORMAT_B8G8R8A8_UNORM,
                                         D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET);
    }
    D3D11_BOX box{0, 0, 0, cw, ch, 1};
    device_->context()->CopySubresourceRegion(texture_.get(), 0, 0, 0, 0, frame_texture.get(), 0, &box);
  }
  content_ = SIZE{static_cast<LONG>(cw), static_cast<LONG>(ch)};
  latest.Close();

  if (content.Width > 0 && content.Height > 0 &&
      (content.Width != pool_size_.Width || content.Height != pool_size_.Height)) {
    // Window resized: grow/shrink the pool so later frames are not clipped.
    pool_size_ = content;
    pool_.Recreate(winrt_device_, DirectXPixelFormat::B8G8R8A8UIntNormalized, 2, pool_size_);
  }
  ++frames_received_;
  return true;
}

void WgcCapture::close() {
  closed_revoker_.revoke();
  if (session_) {
    session_.Close();
    session_ = nullptr;
  }
  if (pool_) {
    pool_.Close();
    pool_ = nullptr;
  }
  item_ = nullptr;
}

// ---------------------------------------------------------------------------------------
// CapturePipeline

CapturePipeline::CapturePipeline(std::shared_ptr<D3DDevice> device, std::shared_ptr<VideoEncoder> encoder,
                                 CaptureTarget target, std::optional<RECT> crop, bool cursor, bool border,
                                 int64_t max_frames)
    : device_(std::move(device)),
      encoder_(std::move(encoder)),
      target_(target),
      crop_(crop),
      cursor_(cursor),
      border_(border),
      max_frames_(max_frames) {}

CapturePipeline::~CapturePipeline() {
  try {
    stop();
  } catch (...) {
  }
}

void CapturePipeline::start() {
  if (thread_.joinable()) throw HrError(E_ILLEGAL_METHOD_CALL, "Pipeline already started");
  set_state("starting");
  auto ready = std::make_shared<std::promise<void>>();
  auto ready_future = ready->get_future();
  thread_ = std::thread([this, ready] { run(ready); });
  try {
    ready_future.get();
  } catch (...) {
    thread_.join();
    set_state("error");
    throw;
  }
}

void CapturePipeline::pause() {
  {
    std::lock_guard<std::mutex> guard(control_mutex_);
    pause_requested_ = true;
  }
  control_cv_.notify_all();
}

void CapturePipeline::resume() {
  {
    std::lock_guard<std::mutex> guard(control_mutex_);
    pause_requested_ = false;
  }
  control_cv_.notify_all();
}

void CapturePipeline::stop() {
  {
    std::lock_guard<std::mutex> guard(control_mutex_);
    stop_requested_ = true;
  }
  control_cv_.notify_all();
  if (thread_.joinable()) thread_.join();
}

PipelineStats CapturePipeline::stats() const {
  std::lock_guard<std::mutex> guard(stats_mutex_);
  return stats_;
}

void CapturePipeline::set_state(const char* state) {
  std::lock_guard<std::mutex> guard(stats_mutex_);
  stats_.state = state;
}

void CapturePipeline::fail(const std::string& kind, const std::string& message) {
  std::lock_guard<std::mutex> guard(stats_mutex_);
  stats_.state = "error";
  stats_.error_kind = kind;
  stats_.error = message;
}

void CapturePipeline::classify_failure(HRESULT hr, const std::string& message) {
  const bool lost = is_device_lost(hr) || FAILED(device_->removed_reason());
  fail(lost ? "device_lost" : "error", message);
}

void CapturePipeline::run(std::shared_ptr<std::promise<void>> ready) {
  ensure_com();
  const UINT fps = encoder_->config().fps;
  std::unique_ptr<WgcCapture> capture;
  com_ptr<ID3D11Texture2D> synthetic;
  try {
    if (target_.kind == SourceKind::Synthetic) {
      std::lock_guard<std::recursive_mutex> guard(device_->lock());
      synthetic = device_->create_texture(target_.synthetic_width, target_.synthetic_height,
                                          DXGI_FORMAT_B8G8R8A8_UNORM,
                                          D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET);
    } else {
      capture = std::make_unique<WgcCapture>(device_, target_, cursor_, border_, fps);
    }
  } catch (...) {
    ready->set_exception(std::current_exception());
    return;
  }
  set_state("running");
  ready->set_value();

  // High-resolution waitable timer (Windows 10 1803+) gives ~0.5 ms pacing accuracy
  // without raising the global timer resolution.
  HANDLE timer = CreateWaitableTimerExW(nullptr, nullptr, CREATE_WAITABLE_TIMER_HIGH_RESOLUTION, TIMER_ALL_ACCESS);
  if (timer == nullptr) timer = CreateWaitableTimerExW(nullptr, nullptr, 0, TIMER_ALL_ACCESS);

  using clock = std::chrono::steady_clock;
  int64_t base_time = qpc_now_hns();
  int64_t base_index = encoder_->last_index() + 1;
  int64_t tick = 0;
  bool have_frame = synthetic != nullptr;
  double total_tick_ms = 0.0;
  int64_t ticks = 0;

  try {
    for (;;) {
      {
        std::unique_lock<std::mutex> lock(control_mutex_);
        if (stop_requested_) break;
        if (pause_requested_) {
          set_state("paused");
          while (pause_requested_ && !stop_requested_) {
            control_cv_.wait_for(lock, std::chrono::milliseconds(50));
            if (capture) {
              // Keep draining WGC so the first frame after resume is current, not stale.
              lock.unlock();
              if (capture->poll()) have_frame = true;
              lock.lock();
            }
          }
          if (stop_requested_) break;
          set_state("running");
          // Paused time is removed from the timeline: continue right after the last frame.
          base_time = qpc_now_hns();
          base_index = encoder_->last_index() + 1;
          tick = 0;
        }
      }

      const int64_t due = base_time + frame_time_hns(tick, fps);
      int64_t now = qpc_now_hns();
      if (due > now) {
        if (timer != nullptr) {
          LARGE_INTEGER relative;
          relative.QuadPart = -(due - now);
          SetWaitableTimer(timer, &relative, 0, nullptr, nullptr, FALSE);
          WaitForSingleObject(timer, 1000);
        } else {
          Sleep(static_cast<DWORD>((due - now) / 10'000));
        }
        now = qpc_now_hns();
      }
      // If we fell behind by whole frame periods, skip those slots (timestamps keep
      // reflecting wall-clock time; the player holds the previous frame).
      const int64_t current_tick = (now - base_time) * fps / 10'000'000;
      if (current_tick > tick) {
        std::lock_guard<std::mutex> guard(stats_mutex_);
        stats_.frames_dropped += current_tick - tick;
        tick = current_tick;
      }

      const auto t0 = clock::now();
      ID3D11Texture2D* source = nullptr;
      RECT rect{};
      int64_t captured = 0;
      int64_t duplicated = 0;
      if (capture) {
        if (capture->poll()) {
          captured = 1;
        } else if (have_frame) {
          duplicated = 1;
        }
        if (capture->closed()) {
          fail("source_closed", "The captured window or monitor is no longer available");
          break;
        }
        source = capture->texture();
        if (source == nullptr) {
          // No frame yet: restart the timeline so frame 0 is the first real image.
          Sleep(2);
          base_time = qpc_now_hns();
          tick = 0;
          continue;
        }
        have_frame = true;
        const SIZE size = capture->content_size();
        rect = RECT{0, 0, size.cx, size.cy};
        if (crop_) {
          RECT r{std::max(crop_->left, 0L), std::max(crop_->top, 0L), std::min(crop_->right, size.cx),
                 std::min(crop_->bottom, size.cy)};
          if (r.right - r.left >= 2 && r.bottom - r.top >= 2) rect = r;
        }
      } else {
        const int64_t index = base_index + tick;
        {
          std::lock_guard<std::recursive_mutex> guard(device_->lock());
          VideoEncoder::render_test_pattern(*device_, synthetic.get(), index);
        }
        source = synthetic.get();
        rect = RECT{0, 0, static_cast<LONG>(target_.synthetic_width), static_cast<LONG>(target_.synthetic_height)};
        captured = 1;
      }

      encoder_->encode_at(source, rect, base_index + tick);
      const double tick_ms = std::chrono::duration<double, std::milli>(clock::now() - t0).count();
      total_tick_ms += tick_ms;
      ++ticks;
      int64_t written = 0;
      {
        std::lock_guard<std::mutex> guard(stats_mutex_);
        stats_.frames_written += 1;
        stats_.frames_captured += captured;
        stats_.frames_duplicated += duplicated;
        stats_.avg_tick_ms = total_tick_ms / static_cast<double>(ticks);
        stats_.max_tick_ms = std::max(stats_.max_tick_ms, tick_ms);
        stats_.recorded_seconds =
            static_cast<double>(frame_time_hns(encoder_->last_index() + 1, fps)) / 10'000'000.0;
        stats_.source_width = rect.right - rect.left;
        stats_.source_height = rect.bottom - rect.top;
        written = stats_.frames_written;
      }
      ++tick;
      if (max_frames_ > 0 && written >= max_frames_) break;
    }
  } catch (const HrError& e) {
    classify_failure(e.hr(), e.what());
  } catch (const winrt::hresult_error& e) {
    classify_failure(e.code(), utf8(std::wstring(e.message())));
  } catch (const std::exception& e) {
    fail("error", e.what());
  }

  if (capture) {
    try {
      capture->close();
    } catch (...) {
    }
  }
  if (timer != nullptr) CloseHandle(timer);
  std::lock_guard<std::mutex> guard(stats_mutex_);
  if (stats_.state != "error") stats_.state = "finished";
}

}  // namespace sr
