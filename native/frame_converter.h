// GPU frame conversion: crop + aspect-preserving scale (letterbox) + pixel format conversion.
//
// Primary path: ID3D11VideoProcessor, which does crop/scale/BGRA->NV12 in one fixed-function
// blit and feeds NV12 directly to the H.264 encoder.
// Fallback path (devices without D3D11 video support): a pixel shader scales into a BGRA
// texture and the IMFSinkWriter inserts its own colour converter (RGB32 input).
#pragma once

#include "d3d_device.h"

#include <memory>
#include <utility>
#include <vector>

namespace sr {

enum class ConverterKind { VideoProcessor, Shader };

// Destination rectangle that fits a src_w x src_h image inside dst_w x dst_h keeping the
// aspect ratio, centred, with even coordinates (required by 4:2:0 chroma).
RECT letterbox_rect(int src_w, int src_h, int dst_w, int dst_h);

class FrameConverter {
 public:
  FrameConverter(std::shared_ptr<D3DDevice> device, UINT out_width, UINT out_height, UINT fps, ConverterKind kind);

  ConverterKind kind() const { return kind_; }
  DXGI_FORMAT output_format() const {
    return kind_ == ConverterKind::VideoProcessor ? DXGI_FORMAT_NV12 : DXGI_FORMAT_B8G8R8A8_UNORM;
  }
  // Bind flags required on destination textures handed to convert().
  UINT output_bind_flags() const { return D3D11_BIND_RENDER_TARGET; }

  // src must be a BGRA texture; src_rect is the region to take (clamped to the texture).
  // Must be called with the device lock held.
  void convert(ID3D11Texture2D* src, RECT src_rect, ID3D11Texture2D* dst);

 private:
  void ensure_video_processor(UINT in_width, UINT in_height);
  void convert_video_processor(ID3D11Texture2D* src, const RECT& src_rect, ID3D11Texture2D* dst);
  void ensure_shader();
  void convert_shader(ID3D11Texture2D* src, const RECT& src_rect, ID3D11Texture2D* dst);

  template <typename View>
  static View* find_cached(std::vector<std::pair<ID3D11Texture2D*, com_ptr<View>>>& cache, ID3D11Texture2D* tex) {
    for (auto& entry : cache) {
      if (entry.first == tex) return entry.second.get();
    }
    return nullptr;
  }
  template <typename View>
  static void store_cached(std::vector<std::pair<ID3D11Texture2D*, com_ptr<View>>>& cache, ID3D11Texture2D* tex,
                           com_ptr<View> view) {
    // Views keep their texture alive, so the raw pointer key cannot be recycled while cached.
    if (cache.size() >= 16) cache.erase(cache.begin());
    cache.emplace_back(tex, std::move(view));
  }

  std::shared_ptr<D3DDevice> device_;
  UINT out_width_;
  UINT out_height_;
  UINT fps_;
  ConverterKind kind_;

  // Video processor state.
  UINT vp_in_width_ = 0;
  UINT vp_in_height_ = 0;
  com_ptr<ID3D11VideoProcessorEnumerator> vp_enum_;
  com_ptr<ID3D11VideoProcessor> vp_;
  std::vector<std::pair<ID3D11Texture2D*, com_ptr<ID3D11VideoProcessorInputView>>> vp_inputs_;
  std::vector<std::pair<ID3D11Texture2D*, com_ptr<ID3D11VideoProcessorOutputView>>> vp_outputs_;

  // Shader state.
  com_ptr<ID3D11VertexShader> vs_;
  com_ptr<ID3D11PixelShader> ps_;
  com_ptr<ID3D11SamplerState> sampler_;
  com_ptr<ID3D11Buffer> constants_;
  std::vector<std::pair<ID3D11Texture2D*, com_ptr<ID3D11ShaderResourceView>>> srvs_;
  std::vector<std::pair<ID3D11Texture2D*, com_ptr<ID3D11RenderTargetView>>> rtvs_;
};

}  // namespace sr
