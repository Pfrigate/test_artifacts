// Windows Graphics Capture source (monitor or window) delivering GPU textures, plus the
// native capture->encode pipeline thread with constant-frame-rate pacing.
#pragma once

#include "d3d_device.h"
#include "mf_encoder.h"

#include <atomic>
#include <condition_variable>
#include <future>
#include <memory>
#include <mutex>
#include <optional>
#include <string>
#include <thread>

#include <winrt/Windows.Foundation.h>
#include <winrt/Windows.Graphics.Capture.h>
#include <winrt/Windows.Graphics.DirectX.h>

namespace sr {

enum class SourceKind { Monitor, Window, Synthetic };

struct CaptureTarget {
  SourceKind kind = SourceKind::Monitor;
  uint64_t handle = 0;   // HMONITOR or HWND
  UINT synthetic_width = 1920;
  UINT synthetic_height = 1080;
};

bool wgc_is_supported();

class WgcCapture {
 public:
  WgcCapture(std::shared_ptr<D3DDevice> device, const CaptureTarget& target, bool cursor, bool border, UINT fps);
  ~WgcCapture();

  // Drains the frame pool; copies the newest frame into an owned texture. Returns true
  // when a new frame arrived since the previous call. Thread-safe w.r.t. the device lock.
  bool poll();
  ID3D11Texture2D* texture() const { return texture_.get(); }
  SIZE content_size() const { return content_; }
  bool closed() const { return closed_.load(); }
  uint64_t frames_received() const { return frames_received_; }
  void close();

 private:
  std::shared_ptr<D3DDevice> device_;
  winrt::Windows::Graphics::DirectX::Direct3D11::IDirect3DDevice winrt_device_{nullptr};
  winrt::Windows::Graphics::Capture::GraphicsCaptureItem item_{nullptr};
  winrt::Windows::Graphics::Capture::Direct3D11CaptureFramePool pool_{nullptr};
  winrt::Windows::Graphics::Capture::GraphicsCaptureSession session_{nullptr};
  winrt::Windows::Graphics::Capture::GraphicsCaptureItem::Closed_revoker closed_revoker_;
  winrt::Windows::Graphics::SizeInt32 pool_size_{};
  com_ptr<ID3D11Texture2D> texture_;
  SIZE content_{};
  std::atomic<bool> closed_{false};
  uint64_t frames_received_ = 0;
};

struct PipelineStats {
  std::string state = "idle";       // idle | starting | running | paused | finished | error
  std::string error_kind;           // "" | device_lost | source_closed | error
  std::string error;
  int64_t frames_written = 0;
  int64_t frames_captured = 0;      // new frames delivered by the source
  int64_t frames_duplicated = 0;    // ticks that re-encoded the previous frame (static content)
  int64_t frames_dropped = 0;       // ticks skipped because the pipeline fell behind
  double avg_tick_ms = 0.0;
  double max_tick_ms = 0.0;
  double recorded_seconds = 0.0;
  int64_t source_width = 0;
  int64_t source_height = 0;
};

class CapturePipeline {
 public:
  CapturePipeline(std::shared_ptr<D3DDevice> device, std::shared_ptr<VideoEncoder> encoder, CaptureTarget target,
                  std::optional<RECT> crop, bool cursor, bool border, int64_t max_frames);
  ~CapturePipeline();

  // Starts the worker thread and waits until the capture source is initialised; throws
  // if the source could not be created.
  void start();
  void pause();
  void resume();
  void stop();  // joins the worker; the encoder is finalised by the caller
  PipelineStats stats() const;

 private:
  void run(std::shared_ptr<std::promise<void>> ready);
  void set_state(const char* state);
  void fail(const std::string& kind, const std::string& message);
  void classify_failure(HRESULT hr, const std::string& message);

  std::shared_ptr<D3DDevice> device_;
  std::shared_ptr<VideoEncoder> encoder_;
  CaptureTarget target_;
  std::optional<RECT> crop_;
  bool cursor_;
  bool border_;
  int64_t max_frames_;

  std::thread thread_;
  std::mutex control_mutex_;
  std::condition_variable control_cv_;
  bool stop_requested_ = false;
  bool pause_requested_ = false;

  mutable std::mutex stats_mutex_;
  PipelineStats stats_;
};

}  // namespace sr
