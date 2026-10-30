// Media Foundation capability detection and MP4 inspection (no FFmpeg / ffprobe).
#pragma once

#include "com_util.h"

#include <string>
#include <tuple>
#include <vector>

namespace sr {

struct EncoderMode {
  UINT width;
  UINT height;
  UINT fps;
  bool supported;
};

struct EncoderInfo {
  std::wstring name;
  std::wstring clsid;
  bool hardware = false;
  bool async = false;
  std::wstring vendor_id;  // "VEN_10DE" etc. for hardware MFTs
  std::vector<EncoderMode> modes;
  std::string error;
};

// Lists H.264 encoder MFTs (hardware first, as sorted by Media Foundation). When
// `test_modes` is true each encoder is activated and asked to accept 720p/1080p/2160p at
// 15/30/60 fps output types.
std::vector<EncoderInfo> enumerate_h264_encoders(bool test_modes);

struct ProbeResult {
  std::string codec;
  UINT width = 0;
  UINT height = 0;
  UINT fps_num = 0;
  UINT fps_den = 0;
  UINT profile = 0;
  int64_t duration_hns = 0;
  int64_t frame_count = -1;
  int64_t keyframe_count = -1;
  int64_t first_pts_hns = 0;
  int64_t last_pts_hns = 0;
  bool timestamps_unique = true;
  bool has_audio = false;
};

// Friendly names of the H.264 decoder MFTs registered on this machine.
std::vector<std::wstring> enumerate_h264_decoders();

ProbeResult probe_file(const std::wstring& path, bool count_frames);

// Decodes one frame near `position_hns` to top-down BGRA (alpha forced to 255).
std::tuple<std::string, UINT, UINT> decode_thumbnail(const std::wstring& path, int64_t position_hns);

}  // namespace sr
