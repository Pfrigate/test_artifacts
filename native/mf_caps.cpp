#include <initguid.h>

#include "mf_caps.h"

#include <codecapi.h>
#include <mfapi.h>
#include <mferror.h>
#include <mfidl.h>
#include <mfreadwrite.h>
#include <mftransform.h>

#include <algorithm>
#include <cstdio>
#include <cstring>

namespace sr {

namespace {

std::wstring get_string(IMFAttributes* attrs, const GUID& key) {
  wchar_t* text = nullptr;
  UINT32 length = 0;
  if (FAILED(attrs->GetAllocatedString(key, &text, &length)) || text == nullptr) return {};
  std::wstring result(text, length);
  CoTaskMemFree(text);
  return result;
}

bool try_output_type(IMFTransform* transform, UINT w, UINT h, UINT fps) {
  com_ptr<IMFMediaType> type;
  if (FAILED(MFCreateMediaType(type.put()))) return false;
  type->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
  type->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_H264);
  type->SetUINT32(MF_MT_AVG_BITRATE, std::max<UINT>(2'000'000, w * h * fps / 10));
  type->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
  type->SetUINT32(MF_MT_MPEG2_PROFILE, eAVEncH264VProfile_High);
  MFSetAttributeSize(type.get(), MF_MT_FRAME_SIZE, w, h);
  MFSetAttributeRatio(type.get(), MF_MT_FRAME_RATE, fps, 1);
  MFSetAttributeRatio(type.get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
  if (FAILED(transform->SetOutputType(0, type.get(), 0))) return false;
  // The output type alone is accepted by most encoders; confirming an NV12 input type
  // catches encoders whose limits are only enforced on the input side.
  com_ptr<IMFMediaType> input;
  if (FAILED(MFCreateMediaType(input.put()))) return false;
  input->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
  input->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12);
  input->SetUINT32(MF_MT_INTERLACE_MODE, MFVideoInterlace_Progressive);
  MFSetAttributeSize(input.get(), MF_MT_FRAME_SIZE, w, h);
  MFSetAttributeRatio(input.get(), MF_MT_FRAME_RATE, fps, 1);
  MFSetAttributeRatio(input.get(), MF_MT_PIXEL_ASPECT_RATIO, 1, 1);
  return SUCCEEDED(transform->SetInputType(0, input.get(), 0));
}

}  // namespace

std::vector<EncoderInfo> enumerate_h264_encoders(bool test_modes) {
  ensure_mf();
  MFT_REGISTER_TYPE_INFO output{MFMediaType_Video, MFVideoFormat_H264};
  IMFActivate** activates = nullptr;
  UINT32 count = 0;
  const UINT32 flags = MFT_ENUM_FLAG_HARDWARE | MFT_ENUM_FLAG_SYNCMFT | MFT_ENUM_FLAG_ASYNCMFT |
                       MFT_ENUM_FLAG_LOCALMFT | MFT_ENUM_FLAG_SORTANDFILTER;
  check(MFTEnumEx(MFT_CATEGORY_VIDEO_ENCODER, flags, nullptr, &output, &activates, &count), "MFTEnumEx");

  std::vector<EncoderInfo> result;
  for (UINT32 i = 0; i < count; ++i) {
    com_ptr<IMFActivate> activate;
    activate.attach(activates[i]);
    EncoderInfo info;
    info.name = get_string(activate.get(), MFT_FRIENDLY_NAME_Attribute);
    info.vendor_id = get_string(activate.get(), MFT_ENUM_HARDWARE_VENDOR_ID_Attribute);
    info.hardware = !get_string(activate.get(), MFT_ENUM_HARDWARE_URL_Attribute).empty();
    UINT32 mft_flags = 0;
    if (SUCCEEDED(activate->GetUINT32(MF_TRANSFORM_FLAGS_Attribute, &mft_flags))) {
      info.async = (mft_flags & MFT_ENUM_FLAG_ASYNCMFT) != 0;
    }
    GUID clsid = GUID_NULL;
    if (SUCCEEDED(activate->GetGUID(MFT_TRANSFORM_CLSID_Attribute, &clsid))) {
      wchar_t buffer[64] = {};
      StringFromGUID2(clsid, buffer, 64);
      info.clsid = buffer;
    }

    if (test_modes) {
      com_ptr<IMFTransform> transform;
      HRESULT hr = activate->ActivateObject(IID_PPV_ARGS(transform.put()));
      if (SUCCEEDED(hr)) {
        if (info.async) {
          com_ptr<IMFAttributes> attrs;
          if (SUCCEEDED(transform->GetAttributes(attrs.put())) && attrs) {
            attrs->SetUINT32(MF_TRANSFORM_ASYNC_UNLOCK, TRUE);
          }
        }
        const UINT sizes[3][2] = {{1280, 720}, {1920, 1080}, {3840, 2160}};
        const UINT rates[3] = {15, 30, 60};
        for (const auto& size : sizes) {
          for (UINT fps : rates) {
            bool ok = try_output_type(transform.get(), size[0], size[1], fps);
            info.modes.push_back(EncoderMode{size[0], size[1], fps, ok});
          }
        }
        transform = nullptr;
        activate->ShutdownObject();
      } else {
        info.error = "ActivateObject failed: " + hr_to_string(hr);
      }
    }
    result.push_back(std::move(info));
  }
  CoTaskMemFree(activates);
  return result;
}

std::vector<std::wstring> enumerate_h264_decoders() {
  ensure_mf();
  MFT_REGISTER_TYPE_INFO input{MFMediaType_Video, MFVideoFormat_H264};
  IMFActivate** activates = nullptr;
  UINT32 count = 0;
  const UINT32 flags = MFT_ENUM_FLAG_HARDWARE | MFT_ENUM_FLAG_SYNCMFT | MFT_ENUM_FLAG_ASYNCMFT |
                       MFT_ENUM_FLAG_LOCALMFT | MFT_ENUM_FLAG_SORTANDFILTER;
  check(MFTEnumEx(MFT_CATEGORY_VIDEO_DECODER, flags, &input, nullptr, &activates, &count), "MFTEnumEx(decoder)");
  std::vector<std::wstring> names;
  for (UINT32 i = 0; i < count; ++i) {
    names.push_back(get_string(activates[i], MFT_FRIENDLY_NAME_Attribute));
    activates[i]->Release();
  }
  CoTaskMemFree(activates);
  return names;
}

ProbeResult probe_file(const std::wstring& path, bool count_frames) {
  ensure_mf();
  com_ptr<IMFSourceReader> reader;
  check(MFCreateSourceReaderFromURL(path.c_str(), nullptr, reader.put()), "MFCreateSourceReaderFromURL");

  com_ptr<IMFMediaType> type;
  check(reader->GetNativeMediaType(MF_SOURCE_READER_FIRST_VIDEO_STREAM, 0, type.put()), "GetNativeMediaType(video)");

  ProbeResult r;
  GUID subtype = GUID_NULL;
  type->GetGUID(MF_MT_SUBTYPE, &subtype);
  if (subtype == MFVideoFormat_H264 || subtype == MFVideoFormat_H264_ES) {
    r.codec = "h264";
  } else {
    char fourcc[5] = {};
    std::memcpy(fourcc, &subtype.Data1, 4);
    r.codec = fourcc;
  }
  MFGetAttributeSize(type.get(), MF_MT_FRAME_SIZE, &r.width, &r.height);
  MFGetAttributeRatio(type.get(), MF_MT_FRAME_RATE, &r.fps_num, &r.fps_den);
  r.profile = MFGetAttributeUINT32(type.get(), MF_MT_MPEG2_PROFILE, 0);

  PROPVARIANT var;
  PropVariantInit(&var);
  if (SUCCEEDED(reader->GetPresentationAttribute(MF_SOURCE_READER_MEDIASOURCE, MF_PD_DURATION, &var))) {
    r.duration_hns = static_cast<int64_t>(var.uhVal.QuadPart);
  }
  PropVariantClear(&var);

  com_ptr<IMFMediaType> audio;
  r.has_audio = SUCCEEDED(reader->GetNativeMediaType(MF_SOURCE_READER_FIRST_AUDIO_STREAM, 0, audio.put()));

  if (count_frames) {
    reader->SetStreamSelection(static_cast<DWORD>(MF_SOURCE_READER_ALL_STREAMS), FALSE);
    reader->SetStreamSelection(static_cast<DWORD>(MF_SOURCE_READER_FIRST_VIDEO_STREAM), TRUE);
    std::vector<int64_t> stamps;
    int64_t keyframes = 0;
    for (;;) {
      DWORD stream = 0;
      DWORD flags = 0;
      LONGLONG timestamp = 0;
      com_ptr<IMFSample> sample;
      check(reader->ReadSample(static_cast<DWORD>(MF_SOURCE_READER_FIRST_VIDEO_STREAM), 0, &stream, &flags, &timestamp,
                               sample.put()),
            "IMFSourceReader::ReadSample");
      if (sample) {
        stamps.push_back(timestamp);
        if (MFGetAttributeUINT32(sample.get(), MFSampleExtension_CleanPoint, 0) != 0) ++keyframes;
      }
      if ((flags & MF_SOURCE_READERF_ENDOFSTREAM) != 0 || (flags & MF_SOURCE_READERF_ERROR) != 0) break;
    }
    r.frame_count = static_cast<int64_t>(stamps.size());
    r.keyframe_count = keyframes;
    if (!stamps.empty()) {
      // Decode order differs from presentation order when B-frames are used.
      std::sort(stamps.begin(), stamps.end());
      r.first_pts_hns = stamps.front();
      r.last_pts_hns = stamps.back();
      r.timestamps_unique = std::adjacent_find(stamps.begin(), stamps.end()) == stamps.end();
    }
  }
  return r;
}

namespace {

// Converts a decoded NV12 sample to top-down BGRA (BT.709 limited range -> full RGB).
std::tuple<std::string, UINT, UINT> nv12_sample_to_bgra(IMFSample* sample, IMFMediaType* type) {
  // MF_MT_FRAME_SIZE is the coded (possibly 16-line padded) size that defines the plane
  // layout; the display aperture is the visible picture.
  UINT32 coded_width = 0, coded_height = 0;
  MFGetAttributeSize(type, MF_MT_FRAME_SIZE, &coded_width, &coded_height);
  if (coded_width == 0 || coded_height == 0) throw HrError(E_UNEXPECTED, "Decoder reported an empty frame size");
  UINT32 width = coded_width, height = coded_height;
  MFVideoArea area{};
  if (SUCCEEDED(type->GetBlob(MF_MT_MINIMUM_DISPLAY_APERTURE, reinterpret_cast<UINT8*>(&area), sizeof(area), nullptr)) &&
      area.Area.cx > 0 && area.Area.cy > 0) {
    width = std::min<UINT32>(width, static_cast<UINT32>(area.Area.cx));
    height = std::min<UINT32>(height, static_cast<UINT32>(area.Area.cy));
  }

  com_ptr<IMFMediaBuffer> buffer;
  check(sample->ConvertToContiguousBuffer(buffer.put()), "ConvertToContiguousBuffer");
  BYTE* data = nullptr;
  LONG pitch = 0;
  com_ptr<IMF2DBuffer> buffer2d = buffer.try_as<IMF2DBuffer>();
  if (buffer2d) {
    check(buffer2d->Lock2D(&data, &pitch), "IMF2DBuffer::Lock2D");
  } else {
    DWORD length = 0;
    check(buffer->Lock(&data, nullptr, &length), "IMFMediaBuffer::Lock");
    pitch = static_cast<LONG>(MFGetAttributeUINT32(type, MF_MT_DEFAULT_STRIDE, coded_width));
  }
  if (pitch <= 0) pitch = static_cast<LONG>(coded_width);

  const BYTE* y_plane = data;
  const BYTE* uv_plane = data + static_cast<ptrdiff_t>(pitch) * coded_height;
  std::string pixels(static_cast<size_t>(width) * height * 4, '\0');
  auto clamp8 = [](int v) { return static_cast<char>(v < 0 ? 0 : (v > 255 ? 255 : v)); };
  for (UINT32 yy = 0; yy < height; ++yy) {
    const BYTE* yrow = y_plane + static_cast<ptrdiff_t>(yy) * pitch;
    const BYTE* uvrow = uv_plane + static_cast<ptrdiff_t>(yy / 2) * pitch;
    char* out = &pixels[static_cast<size_t>(yy) * width * 4];
    for (UINT32 xx = 0; xx < width; ++xx) {
      const int c = (static_cast<int>(yrow[xx]) - 16) * 298;
      const int d = static_cast<int>(uvrow[xx & ~1u]) - 128;
      const int e = static_cast<int>(uvrow[(xx & ~1u) + 1]) - 128;
      out[xx * 4 + 0] = clamp8((c + 541 * d + 128) >> 8);           // B
      out[xx * 4 + 1] = clamp8((c - 55 * d - 136 * e + 128) >> 8);  // G
      out[xx * 4 + 2] = clamp8((c + 459 * e + 128) >> 8);           // R
      out[xx * 4 + 3] = static_cast<char>(0xFF);
    }
  }
  if (buffer2d) {
    buffer2d->Unlock2D();
  } else {
    buffer->Unlock();
  }
  return {std::move(pixels), width, height};
}

com_ptr<IMFMediaType> find_nv12_output(IMFTransform* decoder) {
  for (DWORD i = 0;; ++i) {
    com_ptr<IMFMediaType> type;
    if (FAILED(decoder->GetOutputAvailableType(0, i, type.put()))) return nullptr;
    GUID subtype = GUID_NULL;
    type->GetGUID(MF_MT_SUBTYPE, &subtype);
    if (subtype == MFVideoFormat_NV12) return type;
  }
}

// Drives the H.264 decoder MFT directly with compressed samples from the source reader.
// Used when the Source Reader's own decode path yields nothing, and reports the exact
// HRESULT of the failing step otherwise.
std::tuple<std::string, UINT, UINT> decode_with_mft(const std::wstring& path) {
  com_ptr<IMFSourceReader> reader;
  check(MFCreateSourceReaderFromURL(path.c_str(), nullptr, reader.put()), "MFCreateSourceReaderFromURL");
  const DWORD video = static_cast<DWORD>(MF_SOURCE_READER_FIRST_VIDEO_STREAM);
  reader->SetStreamSelection(static_cast<DWORD>(MF_SOURCE_READER_ALL_STREAMS), FALSE);
  check(reader->SetStreamSelection(video, TRUE), "SetStreamSelection(video)");
  com_ptr<IMFMediaType> input_type;
  check(reader->GetNativeMediaType(video, 0, input_type.put()), "GetNativeMediaType");

  MFT_REGISTER_TYPE_INFO input{MFMediaType_Video, MFVideoFormat_H264};
  IMFActivate** activates = nullptr;
  UINT32 count = 0;
  check(MFTEnumEx(MFT_CATEGORY_VIDEO_DECODER, MFT_ENUM_FLAG_SYNCMFT | MFT_ENUM_FLAG_SORTANDFILTER, &input, nullptr,
                  &activates, &count),
        "MFTEnumEx(decoder)");
  com_ptr<IMFTransform> decoder;
  HRESULT hr = count > 0 ? activates[0]->ActivateObject(IID_PPV_ARGS(decoder.put())) : MF_E_TOPO_CODEC_NOT_FOUND;
  for (UINT32 i = 0; i < count; ++i) activates[i]->Release();
  CoTaskMemFree(activates);
  check(hr, "Activate H.264 decoder");

  check(decoder->SetInputType(0, input_type.get(), 0), "Decoder SetInputType");
  com_ptr<IMFMediaType> output_type = find_nv12_output(decoder.get());
  if (!output_type) throw HrError(MF_E_INVALIDMEDIATYPE, "Decoder offers no NV12 output");
  check(decoder->SetOutputType(0, output_type.get(), 0), "Decoder SetOutputType");
  decoder->ProcessMessage(MFT_MESSAGE_NOTIFY_BEGIN_STREAMING, 0);
  decoder->ProcessMessage(MFT_MESSAGE_NOTIFY_START_OF_STREAM, 0);

  auto try_output = [&](com_ptr<IMFSample>& result) -> HRESULT {
    for (;;) {
      MFT_OUTPUT_STREAM_INFO info{};
      check(decoder->GetOutputStreamInfo(0, &info), "GetOutputStreamInfo");
      com_ptr<IMFSample> out_sample;
      if ((info.dwFlags & (MFT_OUTPUT_STREAM_PROVIDES_SAMPLES | MFT_OUTPUT_STREAM_CAN_PROVIDE_SAMPLES)) == 0) {
        com_ptr<IMFMediaBuffer> out_buffer;
        check(MFCreateMemoryBuffer(std::max<DWORD>(info.cbSize, 1), out_buffer.put()), "MFCreateMemoryBuffer");
        check(MFCreateSample(out_sample.put()), "MFCreateSample");
        out_sample->AddBuffer(out_buffer.get());
      }
      MFT_OUTPUT_DATA_BUFFER out{};
      out.pSample = out_sample.get();
      DWORD status = 0;
      HRESULT phr = decoder->ProcessOutput(0, 1, &out, &status);
      if (out.pEvents) out.pEvents->Release();
      if (phr == MF_E_TRANSFORM_STREAM_CHANGE) {
        output_type = find_nv12_output(decoder.get());
        if (!output_type) return MF_E_INVALIDMEDIATYPE;
        check(decoder->SetOutputType(0, output_type.get(), 0), "Decoder SetOutputType (stream change)");
        continue;
      }
      if (SUCCEEDED(phr)) {
        if (out_sample) {
          result = out_sample;  // we allocated it; out.pSample is a borrowed pointer
        } else {
          result.attach(out.pSample);  // the decoder allocated it and handed us its reference
        }
      } else if (!out_sample && out.pSample != nullptr) {
        out.pSample->Release();
      }
      return phr;
    }
  };

  HRESULT last_input = S_OK;
  HRESULT last_output = S_OK;
  int fed = 0;
  for (int i = 0; i < 600; ++i) {
    DWORD flags = 0;
    com_ptr<IMFSample> compressed;
    check(reader->ReadSample(video, 0, nullptr, &flags, nullptr, compressed.put()), "ReadSample(compressed)");
    if (compressed) {
      last_input = decoder->ProcessInput(0, compressed.get(), 0);
      ++fed;
      com_ptr<IMFSample> decoded;
      last_output = try_output(decoded);
      if (decoded) return nv12_sample_to_bgra(decoded.get(), output_type.get());
      if (FAILED(last_input) && last_input != MF_E_NOTACCEPTING) break;
    }
    if ((flags & MF_SOURCE_READERF_ENDOFSTREAM) != 0) break;
  }
  decoder->ProcessMessage(MFT_MESSAGE_COMMAND_DRAIN, 0);
  com_ptr<IMFSample> decoded;
  last_output = try_output(decoded);
  if (decoded) return nv12_sample_to_bgra(decoded.get(), output_type.get());

  char detail[192];
  std::snprintf(detail, sizeof(detail),
                "H.264 decoder produced no frame (fed=%d, ProcessInput=0x%08lX, ProcessOutput=0x%08lX)", fed,
                static_cast<unsigned long>(last_input), static_cast<unsigned long>(last_output));
  throw HrError(FAILED(last_input) ? last_input : (FAILED(last_output) ? last_output : MF_E_END_OF_STREAM), detail);
}

}  // namespace

std::tuple<std::string, UINT, UINT> decode_thumbnail(const std::wstring& path, int64_t position_hns) {
  ensure_mf();
  com_ptr<IMFSourceReader> reader;
  check(MFCreateSourceReaderFromURL(path.c_str(), nullptr, reader.put()), "MFCreateSourceReaderFromURL");
  const DWORD video = static_cast<DWORD>(MF_SOURCE_READER_FIRST_VIDEO_STREAM);
  reader->SetStreamSelection(static_cast<DWORD>(MF_SOURCE_READER_ALL_STREAMS), FALSE);
  check(reader->SetStreamSelection(video, TRUE), "SetStreamSelection(video)");

  // NV12 is the H.264 decoder's native output, so no colour-converter MFT is inserted;
  // the small thumbnail is converted to BGRA on the CPU.
  com_ptr<IMFMediaType> nv12;
  check(MFCreateMediaType(nv12.put()), "MFCreateMediaType");
  nv12->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
  nv12->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_NV12);
  check(reader->SetCurrentMediaType(video, nullptr, nv12.get()), "SetCurrentMediaType(NV12)");

  if (position_hns > 0) {
    PROPVARIANT var;
    PropVariantInit(&var);
    var.vt = VT_I8;
    var.hVal.QuadPart = position_hns;
    reader->SetCurrentPosition(GUID_NULL, var);
  }

  com_ptr<IMFSample> sample;
  for (int reads = 0; reads < 240 && !sample; ++reads) {
    DWORD flags = 0;
    check(reader->ReadSample(video, 0, nullptr, &flags, nullptr, sample.put()), "IMFSourceReader::ReadSample");
    if ((flags & (MF_SOURCE_READERF_ENDOFSTREAM | MF_SOURCE_READERF_ERROR)) != 0) break;
  }
  if (sample) {
    com_ptr<IMFMediaType> current;
    check(reader->GetCurrentMediaType(video, current.put()), "GetCurrentMediaType");
    return nv12_sample_to_bgra(sample.get(), current.get());
  }
  reader = nullptr;
  return decode_with_mft(path);
}

}  // namespace sr
