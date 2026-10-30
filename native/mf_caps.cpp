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

std::tuple<std::string, UINT, UINT> decode_thumbnail(const std::wstring& path, int64_t position_hns) {
  ensure_mf();
  com_ptr<IMFAttributes> attrs;
  check(MFCreateAttributes(attrs.put(), 1), "MFCreateAttributes");
  attrs->SetUINT32(MF_SOURCE_READER_ENABLE_VIDEO_PROCESSING, TRUE);
  com_ptr<IMFSourceReader> reader;
  check(MFCreateSourceReaderFromURL(path.c_str(), attrs.get(), reader.put()), "MFCreateSourceReaderFromURL");
  const DWORD video = static_cast<DWORD>(MF_SOURCE_READER_FIRST_VIDEO_STREAM);

  com_ptr<IMFMediaType> rgb;
  check(MFCreateMediaType(rgb.put()), "MFCreateMediaType");
  rgb->SetGUID(MF_MT_MAJOR_TYPE, MFMediaType_Video);
  rgb->SetGUID(MF_MT_SUBTYPE, MFVideoFormat_RGB32);
  check(reader->SetCurrentMediaType(video, nullptr, rgb.get()), "SetCurrentMediaType(RGB32)");

  com_ptr<IMFMediaType> current;
  check(reader->GetCurrentMediaType(video, current.put()), "GetCurrentMediaType");
  UINT32 width = 0, height = 0;
  MFGetAttributeSize(current.get(), MF_MT_FRAME_SIZE, &width, &height);

  if (position_hns > 0) {
    PROPVARIANT var;
    PropVariantInit(&var);
    var.vt = VT_I8;
    var.hVal.QuadPart = position_hns;
    reader->SetCurrentPosition(GUID_NULL, var);
  }

  com_ptr<IMFSample> sample;
  DWORD flags = 0;
  int reads = 0;
  for (; reads < 240 && !sample; ++reads) {
    flags = 0;
    check(reader->ReadSample(video, 0, nullptr, &flags, nullptr, sample.put()), "IMFSourceReader::ReadSample");
    if ((flags & (MF_SOURCE_READERF_ENDOFSTREAM | MF_SOURCE_READERF_ERROR)) != 0) break;
  }
  if (!sample && position_hns > 0) {
    // Seeking can land past the last decodable frame of very short clips: retry from 0.
    return decode_thumbnail(path, 0);
  }
  if (!sample) {
    char detail[96];
    std::snprintf(detail, sizeof(detail), "No decodable video frame found (reads=%d, flags=0x%lX)", reads,
                  static_cast<unsigned long>(flags));
    throw HrError(MF_E_END_OF_STREAM, detail);
  }

  com_ptr<IMFMediaBuffer> buffer;
  check(sample->GetBufferByIndex(0, buffer.put()), "GetBufferByIndex");
  std::string pixels(static_cast<size_t>(width) * height * 4, '\0');

  if (auto buffer2d = buffer.try_as<IMF2DBuffer>()) {
    BYTE* scan0 = nullptr;
    LONG pitch = 0;
    check(buffer2d->Lock2D(&scan0, &pitch), "IMF2DBuffer::Lock2D");
    for (UINT32 y = 0; y < height; ++y) {
      std::memcpy(&pixels[static_cast<size_t>(y) * width * 4], scan0 + static_cast<ptrdiff_t>(y) * pitch,
                  static_cast<size_t>(width) * 4);
    }
    buffer2d->Unlock2D();
  } else {
    BYTE* data = nullptr;
    DWORD length = 0;
    check(buffer->Lock(&data, nullptr, &length), "IMFMediaBuffer::Lock");
    LONG stride = static_cast<LONG>(MFGetAttributeUINT32(current.get(), MF_MT_DEFAULT_STRIDE, width * 4));
    for (UINT32 y = 0; y < height; ++y) {
      const ptrdiff_t row = stride >= 0 ? static_cast<ptrdiff_t>(y) * stride
                                        : static_cast<ptrdiff_t>(height - 1 - y) * (-stride);
      if (row + static_cast<ptrdiff_t>(width) * 4 > static_cast<ptrdiff_t>(length)) break;
      std::memcpy(&pixels[static_cast<size_t>(y) * width * 4], data + row, static_cast<size_t>(width) * 4);
    }
    buffer->Unlock();
  }
  for (size_t i = 3; i < pixels.size(); i += 4) pixels[i] = static_cast<char>(0xFF);
  return {std::move(pixels), width, height};
}

}  // namespace sr
