// initguid.h first: defines the MF / CodecAPI GUIDs used here as selectany symbols so the
// module does not depend on every GUID being present in mfuuid.lib.
#include <initguid.h>

#include "mf_encoder.h"

#include <codecapi.h>
#include <evr.h>
#include <mferror.h>

#include <algorithm>
#include <atomic>
#include <vector>

namespace sr {

void ensure_mf() {
  static std::once_flag once;
  static HRESULT startup_hr = S_OK;
  ensure_com();
  std::call_once(once, [] { startup_hr = MFStartup(MF_VERSION, MFSTARTUP_FULL); });
  check(startup_hr, "MFStartup");
}

// ---------------------------------------------------------------------------------------
// SurfacePool

struct SurfacePool::Callback : winrt::implements<SurfacePool::Callback, IMFAsyncCallback> {
  explicit Callback(SurfacePool* pool) : pool(pool) {}

  HRESULT __stdcall GetParameters(DWORD*, DWORD*) noexcept override { return E_NOTIMPL; }

  HRESULT __stdcall Invoke(IMFAsyncResult* result) noexcept override {
    // IMFTrackedSample hands itself back through the result object once the last
    // external reference is released.
    com_ptr<::IUnknown> object;
    if (FAILED(result->GetObject(object.put())) || !object) return S_OK;
    auto sample = object.try_as<IMFSample>();
    SurfacePool* owner = pool.load();
    if (sample && owner != nullptr) owner->release(sample.get());
    return S_OK;
  }

  std::atomic<SurfacePool*> pool;
};

SurfacePool::SurfacePool(D3DDevice& device, UINT width, UINT height, DXGI_FORMAT format, UINT bind_flags,
                         size_t capacity)
    : capacity_(capacity) {
  callback_ = winrt::make_self<Callback>(this);
  for (size_t i = 0; i < capacity; ++i) {
    auto texture = device.create_texture(width, height, format, bind_flags);
    com_ptr<IMFMediaBuffer> buffer;
    check(MFCreateDXGISurfaceBuffer(__uuidof(ID3D11Texture2D), texture.get(), 0, FALSE, buffer.put()),
          "MFCreateDXGISurfaceBuffer");
    if (auto buffer2d = buffer.try_as<IMF2DBuffer>()) {
      DWORD length = 0;
      if (SUCCEEDED(buffer2d->GetContiguousLength(&length))) buffer->SetCurrentLength(length);
    }
    com_ptr<IMFSample> sample;
    check(MFCreateVideoSampleFromSurface(nullptr, sample.put()), "MFCreateVideoSampleFromSurface");
    check(sample->AddBuffer(buffer.get()), "IMFSample::AddBuffer");
    free_.push_back(std::move(sample));
  }
}

SurfacePool::~SurfacePool() {
  wait_all_returned(std::chrono::milliseconds(2000));
  callback_->pool.store(nullptr);
  std::lock_guard<std::mutex> guard(mutex_);
  free_.clear();
}

com_ptr<IMFSample> SurfacePool::acquire(std::chrono::milliseconds timeout) {
  std::unique_lock<std::mutex> lock(mutex_);
  if (!cv_.wait_for(lock, timeout, [this] { return !free_.empty(); })) {
    throw HrError(HRESULT_FROM_WIN32(WAIT_TIMEOUT), "Encoder surface pool exhausted (encoder stalled)");
  }
  com_ptr<IMFSample> sample = std::move(free_.front());
  free_.pop_front();
  lock.unlock();
  auto tracked = sample.as<IMFTrackedSample>();
  check(tracked->SetAllocator(callback_.get(), nullptr), "IMFTrackedSample::SetAllocator");
  return sample;
}

void SurfacePool::release(IMFSample* sample) {
  com_ptr<IMFSample> ref;
  ref.copy_from(sample);
  {
    std::lock_guard<std::mutex> guard(mutex_);
    free_.push_back(std::move(ref));
  }
  cv_.notify_all();
}

com_ptr<ID3D11Texture2D> SurfacePool::texture_of(IMFSample* sample) {
  com_ptr<IMFMediaBuffer> buffer;
  check(sample->GetBufferByIndex(0, buffer.put()), "IMFSample::GetBufferByIndex");
  auto dxgi_buffer = buffer.as<IMFDXGIBuffer>();
  com_ptr<ID3D11Texture2D> texture;
  check(dxgi_buffer->GetResource(IID_PPV_ARGS(texture.put())), "IMFDXGIBuffer::GetResource");
  return texture;
}

bool SurfacePool::wait_all_returned(std::chrono::milliseconds timeout) {
  std::unique_lock<std::mutex> lock(mutex_);
  return cv_.wait_for(lock, timeout, [this] { return free_.size() >= capacity_; });
}

// ---------------------------------------------------------------------------------------
// VideoEncoder

namespace {

UINT profile_value(const std::string& name) {
  if (name == "baseline") return eAVEncH264VProfile_Base;
  if (name == "main") return eAVEncH264VProfile_Main;
  return eAVEncH264VProfile_High;
}

const char* profile_name(UINT value) {
  switch (value) {
    case eAVEncH264VProfile_Base: return "baseline";
    case eAVEncH264VProfile_Main: return "main";
    default: return "high";
  }
}

}  // namespace

VideoEncoder::VideoEncoder(std::shared_ptr<D3DDevice> device, EncoderConfig config)
    : device_(std::move(device)), config_(std::move(config)) {
  ensure_mf();

  const UINT w = config_.width;
  const UINT h = config_.height;
  if (w < 64 || h < 64 || (w % 2) != 0 || (h % 2) != 0)
    throw HrError(E_INVALIDARG, "Output size must be even and at least 64x64");
  if (w > 4096 || h > 4096 || ((w + 15) / 16) * ((h + 15) / 16) > 36864)
    throw HrError(E_INVALIDARG, "Output size exceeds H.264 level 5.2 limits (max 4096x2304)");
  if (config_.fps < 1 || config_.fps > 120) throw HrError(E_INVALIDARG, "fps must be within 1..120");
  if (config_.pool_size < 2) config_.pool_size = 2;

  // GPU conversion stage.
  if (config_.converter == "shader") {
    converter_ = std::make_unique<FrameConverter>(device_, w, h, config_.fps, ConverterKind::Shader);
  } else if (config_.converter == "video_processor") {
    converter_ = std::make_unique<FrameConverter>(device_, w, h, config_.fps, ConverterKind::VideoProcessor);
  } else {
    try {
      converter_ = std::make_unique<FrameConverter>(device_, w, h, config_.fps, ConverterKind::VideoProcessor);
    } catch (const HrError&) {
      converter_ = std::make_unique<FrameConverter>(device_, w, h, config_.fps, ConverterKind::Shader);
    }
  }
  pool_ = std::make_unique<SurfacePool>(*device_, w, h, converter_->output_format(), converter_->output_bind_flags(),
                                        config_.pool_size);

  check(MFCreateDXGIDeviceManager(&reset_token_, device_manager_.put()), "MFCreateDXGIDeviceManager");
  check(device_manager_->ResetDevice(device_->device(), reset_token_), "IMFDXGIDeviceManager::ResetDevice");

  std::vector<UINT> profiles{profile_value(config_.profile)};
  if (profiles[0] == eAVEncH264VProfile_High) profiles.push_back(eAVEncH264VProfile_Main);
  std::vector<bool> hardware_options;
  if (config_.prefer_hardware) hardware_options.push_back(true);
  hardware_options.push_back(false);

  HrError last_error(E_FAIL, "No H.264 encoder configuration succeeded");
  for (bool hw : hardware_options) {
    for (UINT profile : profiles) {
      try {
        open_writer(hw, profile);
        detect_encoder();
        return;
      } catch (const HrError& e) {
        last_error = e;
      } catch (const winrt::hresult_error& e) {
        last_error = HrError(e.code(), "Sink writer setup failed: " + utf8(std::wstring(e.message())));
      }
      writer_ = nullptr;
      DeleteFileW(config_.path.c_str());
    }
  }
  throw last_error;
}

VideoEncoder::~VideoEncoder() {
  try {
    finalize();
  } catch (...) {
  }
}

void VideoEncoder::open_writer(bool hardware, UINT profile) {
  com_ptr<IMFAttributes> attrs;
  check(MFCreateAttributes(attrs.put(), 5), "MFCreateAttributes");
  check(attrs->SetUINT32(MF_READWRITE_ENABLE_HARDWARE_TRANSFORMS, hardware ? TRUE : FALSE), "SetUINT32");
  check(attrs->SetUnknown(MF_SINK_WRITER_D3D_MANAGER, device_manager_.get()), "SetUnknown(D3D manager)");
  check(attrs->SetGUID(MF_TRANSCODE_CONTAINERTYPE, MFTranscodeContainerType_MPEG4), "SetGUID(container)");
  // moov before mdat ("fast start"): PowerPoint and progressive players can start immediately.
  check(attrs->SetUINT32(MF_MPEG4SINK_MOOV_BEFORE_MDAT, TRUE), "SetUINT32(moov first)");
  check(attrs->SetUINT32(MF_SINK_WRITER_DISABLE_THROTTLING, FALSE), "SetUINT32(throttling)");

  com_ptr<IMFSinkWriter> writer;
  check(MFCreateSinkWriterFromURL(config_.path.c_str(), nullptr, attrs.get(), writer.put()),
        "MFCreateSinkWriterFromURL");

  const UINT w = config_.width;
  const UINT h = config_.height;

  com_ptr<IMFMediaType> out_type;
  check(MFCreateMediaType(out_type.put()), "MFCreateMediaType");
  out_type->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
  out_type->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_H264);
  out_type->SetUINT32(MF_MT_AVG_BITRATE, config_.bitrate);
  out_type->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
  MFSetAttributeSize(out_type.get(), MF_MT_FRAME_SIZE, w, h);
  MFSetAttributeRatio(out_type.get(), MF_MT_FRAME_RATE, config_.fps, 1);
  MFSetAttributeRatio(out_type.get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
  out_type->SetUINT32(MF_MT_MPEG2_PROFILE, profile);
  out_type->SetUINT32(MF_MT_VIDEO_NOMINAL_RANGE, MFNominalRange_16_235);
  out_type->SetUINT32(MF_MT_YUV_MATRIX, MFVideoTransferMatrix_BT709);
  out_type->SetUINT32(MF_MT_VIDEO_PRIMARIES, MFVideoPrimaries_BT709);
  out_type->SetUINT32(MF_MT_TRANSFER_FUNCTION, MFVideoTransFunc_709);
  DWORD stream = 0;
  check(writer->AddStream(out_type.get(), &stream), "IMFSinkWriter::AddStream");

  const bool nv12 = converter_->kind() == ConverterKind::VideoProcessor;
  com_ptr<IMFMediaType> in_type;
  check(MFCreateMediaType(in_type.put()), "MFCreateMediaType");
  in_type->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
  in_type->SetGUID(MF_MT_SUBTYPE, nv12 ? MFVideoFormat_NV12 : MFVideoFormat_RGB32);
  in_type->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
  MFSetAttributeSize(in_type.get(), MF_MT_FRAME_SIZE, w, h);
  MFSetAttributeRatio(in_type.get(), MF_MT_FRAME_RATE, config_.fps, 1);
  MFSetAttributeRatio(in_type.get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
  if (nv12) {
    in_type->SetUINT32(MF_MT_VIDEO_NOMINAL_RANGE, MFNominalRange_16_235);
    in_type->SetUINT32(MF_MT_YUV_MATRIX, MFVideoTransferMatrix_BT709);
  } else {
    // Positive stride = top-down rows, which is how D3D textures are laid out.
    in_type->SetUINT32(MF_MT_DEFAULT_STRIDE, w * 4);
  }

  com_ptr<IMFAttributes> params;
  check(MFCreateAttributes(params.put(), 3), "MFCreateAttributes(params)");
  params->SetUINT32(CODECAPI_AVEncCommonRateControlMode, eAVEncCommonRateControlMode_UnconstrainedVBR);
  params->SetUINT32(CODECAPI_AVEncCommonMeanBitRate, config_.bitrate);
  params->SetUINT32(CODECAPI_AVEncMPVGOPSize, std::max<UINT>(1, config_.fps * config_.gop_seconds));
  HRESULT hr = writer->SetInputMediaType(stream, in_type.get(), params.get());
  if (FAILED(hr)) {
    // Some encoders reject individual CodecAPI settings; MF_MT_AVG_BITRATE still applies.
    hr = writer->SetInputMediaType(stream, in_type.get(), nullptr);
  }
  check(hr, "IMFSinkWriter::SetInputMediaType");
  check(writer->BeginWriting(), "IMFSinkWriter::BeginWriting");

  writer_ = writer;
  stream_index_ = stream;
  hardware_requested_ = hardware;
  profile_used_ = profile_name(profile);
}

void VideoEncoder::detect_encoder() {
  hardware_ = false;
  encoder_name_.clear();
  if (auto ex = writer_.try_as<IMFSinkWriterEx>()) {
    for (DWORD i = 0;; ++i) {
      GUID category = GUID_NULL;
      com_ptr<IMFTransform> transform;
      if (FAILED(ex->GetTransformForStream(stream_index_, i, &category, transform.put()))) break;
      if (category != MFT_CATEGORY_VIDEO_ENCODER || !transform) continue;
      com_ptr<IMFAttributes> attrs;
      if (FAILED(transform->GetAttributes(attrs.put())) || !attrs) continue;
      wchar_t* text = nullptr;
      UINT32 length = 0;
      if (SUCCEEDED(attrs->GetAllocatedString(MFT_ENUM_HARDWARE_URL_Attribute, &text, &length))) {
        hardware_ = true;
        CoTaskMemFree(text);
      }
      text = nullptr;
      if (SUCCEEDED(attrs->GetAllocatedString(MFT_FRIENDLY_NAME_Attribute, &text, &length))) {
        encoder_name_ = text;
        CoTaskMemFree(text);
      }
    }
  }
  if (encoder_name_.empty()) {
    encoder_name_ = hardware_ ? L"Hardware H.264 Encoder MFT" : L"Microsoft H.264 Encoder MFT (software)";
  }
}

void VideoEncoder::encode_locked(ID3D11Texture2D* src, const RECT& src_rect, int64_t index) {
  if (finalized_ || !writer_) throw HrError(MF_E_SHUTDOWN, "Encoder already finalized");
  if (index <= stats_.last_index) throw HrError(E_INVALIDARG, "Frame index must increase monotonically");

  using clock = std::chrono::steady_clock;
  const auto t0 = clock::now();
  com_ptr<IMFSample> sample = pool_->acquire(std::chrono::milliseconds(3000));
  const auto t1 = clock::now();
  {
    auto target = SurfacePool::texture_of(sample.get());
    std::lock_guard<std::recursive_mutex> device_guard(device_->lock());
    converter_->convert(src, src_rect, target.get());
  }
  const auto t2 = clock::now();

  const int64_t time = frame_time_hns(index, config_.fps);
  const int64_t next = frame_time_hns(index + 1, config_.fps);
  check(sample->SetSampleTime(time), "IMFSample::SetSampleTime");
  check(sample->SetSampleDuration(next - time), "IMFSample::SetSampleDuration");
  HRESULT hr = writer_->WriteSample(stream_index_, sample.get());
  sample = nullptr;  // the surface returns to the pool once the encoder releases it
  check(hr, "IMFSinkWriter::WriteSample");
  const auto t3 = clock::now();

  auto ms = [](clock::duration d) { return std::chrono::duration<double, std::milli>(d).count(); };
  stats_.frames_written += 1;
  stats_.last_index = index;
  stats_.total_wait_ms += ms(t1 - t0);
  stats_.total_convert_ms += ms(t2 - t1);
  stats_.total_write_ms += ms(t3 - t2);
  stats_.max_write_ms = std::max(stats_.max_write_ms, ms(t3 - t0));
}

void VideoEncoder::encode_at(ID3D11Texture2D* src, const RECT& src_rect, int64_t index) {
  std::lock_guard<std::mutex> guard(mutex_);
  encode_locked(src, src_rect, index);
}

int64_t VideoEncoder::encode_next(ID3D11Texture2D* src, const RECT& src_rect) {
  std::lock_guard<std::mutex> guard(mutex_);
  const int64_t index = stats_.last_index + 1;
  encode_locked(src, src_rect, index);
  return index;
}

int64_t VideoEncoder::write_bgra(const uint8_t* data, UINT width, UINT height, UINT row_pitch, int64_t index) {
  if (width < 2 || height < 2) throw HrError(E_INVALIDARG, "Frame too small");
  if (row_pitch < width * 4) throw HrError(E_INVALIDARG, "Row pitch smaller than width * 4");
  std::lock_guard<std::mutex> upload_guard(upload_mutex_);
  {
    std::lock_guard<std::recursive_mutex> device_guard(device_->lock());
    D3D11_TEXTURE2D_DESC desc{};
    if (upload_texture_) upload_texture_->GetDesc(&desc);
    if (!upload_texture_ || desc.Width != width || desc.Height != height) {
      upload_texture_ = device_->create_texture(width, height, DXGI_FORMAT_B8G8R8A8_UNORM,
                                                D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET);
    }
    device_->context()->UpdateSubresource(upload_texture_.get(), 0, nullptr, data, row_pitch, 0);
  }
  const RECT full{0, 0, static_cast<LONG>(width), static_cast<LONG>(height)};
  if (index < 0) return encode_next(upload_texture_.get(), full);
  encode_at(upload_texture_.get(), full, index);
  return index;
}

void VideoEncoder::render_test_pattern(D3DDevice& device, ID3D11Texture2D* texture, int64_t index) {
  D3D11_TEXTURE2D_DESC desc{};
  texture->GetDesc(&desc);
  com_ptr<ID3D11RenderTargetView> rtv;
  check(device.device()->CreateRenderTargetView(texture, nullptr, rtv.put()), "CreateRenderTargetView(pattern)");
  const float phase = static_cast<float>(index % 240) / 240.0f;
  const float background[4] = {0.10f + 0.30f * phase, 0.15f, 0.45f - 0.30f * phase, 1.0f};
  auto* ctx = device.context();
  ctx->ClearRenderTargetView(rtv.get(), background);
  com_ptr<ID3D11DeviceContext> ctx_ref;
  ctx_ref.copy_from(ctx);
  if (auto ctx1 = ctx_ref.try_as<ID3D11DeviceContext1>()) {
    const LONG box = std::max<LONG>(8, static_cast<LONG>(desc.Height) / 6);
    const LONG travel = std::max<LONG>(1, static_cast<LONG>(desc.Width) - box);
    const LONG x = static_cast<LONG>((index * 8) % travel);
    const LONG y = (static_cast<LONG>(desc.Height) - box) / 2;
    const D3D11_RECT rect{x, y, x + box, y + box};
    const float white[4] = {1.0f, 1.0f, 1.0f, 1.0f};
    ctx1->ClearView(rtv.get(), white, &rect, 1);
    // A thin progress bar along the bottom encodes the frame number visually.
    const LONG bar = static_cast<LONG>((index % 600) * static_cast<int64_t>(desc.Width) / 600);
    const D3D11_RECT bar_rect{0, static_cast<LONG>(desc.Height) - 8, std::max<LONG>(2, bar),
                              static_cast<LONG>(desc.Height)};
    const float green[4] = {0.1f, 0.9f, 0.3f, 1.0f};
    ctx1->ClearView(rtv.get(), green, &bar_rect, 1);
  }
}

int64_t VideoEncoder::write_synthetic() {
  std::lock_guard<std::mutex> upload_guard(upload_mutex_);
  const int64_t index = last_index() + 1;
  {
    std::lock_guard<std::recursive_mutex> device_guard(device_->lock());
    if (!synthetic_texture_) {
      synthetic_texture_ = device_->create_texture(config_.width, config_.height, DXGI_FORMAT_B8G8R8A8_UNORM,
                                                   D3D11_BIND_SHADER_RESOURCE | D3D11_BIND_RENDER_TARGET);
    }
    render_test_pattern(*device_, synthetic_texture_.get(), index);
  }
  return encode_next(synthetic_texture_.get(),
                     RECT{0, 0, static_cast<LONG>(config_.width), static_cast<LONG>(config_.height)});
}

void VideoEncoder::finalize() {
  std::lock_guard<std::mutex> guard(mutex_);
  if (finalized_) return;
  finalized_ = true;
  HRESULT hr = S_OK;
  if (writer_) {
    hr = writer_->Finalize();
    writer_ = nullptr;
  }
  if (pool_) pool_->wait_all_returned(std::chrono::milliseconds(5000));
  // With zero samples Finalize fails by design (MF_E_SINK_NO_SAMPLES_PROCESSED); the
  // Python layer deletes the empty file in that case.
  if (stats_.frames_written > 0) check(hr, "IMFSinkWriter::Finalize");
}

std::string VideoEncoder::converter_name() const {
  return converter_->kind() == ConverterKind::VideoProcessor ? "video_processor" : "shader";
}

std::string VideoEncoder::input_subtype() const {
  return converter_->kind() == ConverterKind::VideoProcessor ? "NV12" : "RGB32";
}

EncoderStats VideoEncoder::stats() const {
  std::lock_guard<std::mutex> guard(mutex_);
  return stats_;
}

int64_t VideoEncoder::last_index() const {
  std::lock_guard<std::mutex> guard(mutex_);
  return stats_.last_index;
}

}  // namespace sr
