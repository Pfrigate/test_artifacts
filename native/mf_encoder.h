// H.264 / MP4 encoder built on IMFSinkWriter with a DXGI device manager, so captured
// textures are converted and encoded on the GPU without being read back to system memory.
#pragma once

#include "d3d_device.h"
#include "frame_converter.h"

#include <mfapi.h>
#include <mfidl.h>
#include <mfreadwrite.h>

#include <chrono>
#include <condition_variable>
#include <deque>
#include <memory>
#include <mutex>
#include <string>

namespace sr {

struct EncoderConfig {
  std::wstring path;
  UINT width = 1920;
  UINT height = 1080;
  UINT fps = 30;
  UINT bitrate = 8'000'000;
  bool prefer_hardware = true;
  std::string profile = "high";        // "high" | "main" | "baseline"
  UINT gop_seconds = 2;
  std::string converter = "auto";      // "auto" | "video_processor" | "shader"
  UINT pool_size = 6;
};

struct EncoderStats {
  int64_t frames_written = 0;
  int64_t last_index = -1;
  double total_convert_ms = 0.0;
  double total_write_ms = 0.0;
  double max_write_ms = 0.0;
  double total_wait_ms = 0.0;
};

// Pool of GPU surfaces wrapped in tracked IMFSamples. A surface is reused only after the
// encoder releases the sample that references it, so in-flight frames are never overwritten.
class SurfacePool {
 public:
  SurfacePool(D3DDevice& device, UINT width, UINT height, DXGI_FORMAT format, UINT bind_flags, size_t capacity);
  ~SurfacePool();

  // Returns a free sample (blocking up to `timeout`); the caller owns the only reference.
  com_ptr<IMFSample> acquire(std::chrono::milliseconds timeout);
  static com_ptr<ID3D11Texture2D> texture_of(IMFSample* sample);
  bool wait_all_returned(std::chrono::milliseconds timeout);

 private:
  struct Callback;
  void release(IMFSample* sample);

  std::mutex mutex_;
  std::condition_variable cv_;
  std::deque<com_ptr<IMFSample>> free_;
  size_t capacity_;
  com_ptr<Callback> callback_;
};

class VideoEncoder {
 public:
  VideoEncoder(std::shared_ptr<D3DDevice> device, EncoderConfig config);
  ~VideoEncoder();

  VideoEncoder(const VideoEncoder&) = delete;
  VideoEncoder& operator=(const VideoEncoder&) = delete;

  // Converts `src` (BGRA, region `src_rect`) and writes it as frame `index` (CFR timeline).
  void encode_at(ID3D11Texture2D* src, const RECT& src_rect, int64_t index);
  // Writes the next frame index (last_index + 1).
  int64_t encode_next(ID3D11Texture2D* src, const RECT& src_rect);
  // Uploads a CPU BGRA frame (e.g. from DXcam) once and encodes it as frame `index`, or as
  // the next frame when `index` is negative. Returns the index written.
  int64_t write_bgra(const uint8_t* data, UINT width, UINT height, UINT row_pitch, int64_t index = -1);
  // Renders a moving test pattern on the GPU and encodes it as the next frame.
  int64_t write_synthetic();
  void finalize();

  const EncoderConfig& config() const { return config_; }
  bool finalized() const { return finalized_; }
  bool hardware() const { return hardware_; }
  const std::wstring& encoder_name() const { return encoder_name_; }
  std::string converter_name() const;
  std::string input_subtype() const;
  const std::string& profile_used() const { return profile_used_; }
  EncoderStats stats() const;
  int64_t last_index() const;

  // Fills `texture` (BGRA, render-target) with the synthetic pattern for frame `index`.
  static void render_test_pattern(D3DDevice& device, ID3D11Texture2D* texture, int64_t index);

 private:
  void open_writer(bool hardware, UINT profile);
  void detect_encoder();
  void encode_locked(ID3D11Texture2D* src, const RECT& src_rect, int64_t index);

  std::shared_ptr<D3DDevice> device_;
  EncoderConfig config_;
  std::unique_ptr<FrameConverter> converter_;
  std::unique_ptr<SurfacePool> pool_;
  com_ptr<IMFDXGIDeviceManager> device_manager_;
  UINT reset_token_ = 0;
  com_ptr<IMFByteStream> byte_stream_;
  com_ptr<IMFSinkWriter> writer_;
  DWORD stream_index_ = 0;
  bool hardware_ = false;
  bool hardware_requested_ = false;
  std::wstring encoder_name_;
  std::string profile_used_;
  bool finalized_ = false;

  std::mutex upload_mutex_;
  com_ptr<ID3D11Texture2D> upload_texture_;
  com_ptr<ID3D11Texture2D> synthetic_texture_;

  mutable std::mutex mutex_;
  EncoderStats stats_;
};

}  // namespace sr
