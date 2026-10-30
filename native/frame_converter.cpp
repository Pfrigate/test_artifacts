#include "frame_converter.h"

#include <d3dcompiler.h>

#include <algorithm>
#include <cstring>

namespace sr {

namespace {

const char kShaderSource[] = R"(
cbuffer Params : register(b0) { float4 uv_rect; };
Texture2D source_tex : register(t0);
SamplerState linear_sampler : register(s0);
struct VSOut { float4 pos : SV_Position; float2 uv : TEXCOORD0; };
VSOut vs_main(uint id : SV_VertexID) {
  VSOut o;
  float2 t = float2((id << 1) & 2, id & 2);
  o.pos = float4(t * float2(2.0, -2.0) + float2(-1.0, 1.0), 0.0, 1.0);
  o.uv = lerp(uv_rect.xy, uv_rect.zw, t);
  return o;
}
float4 ps_main(VSOut i) : SV_Target {
  return float4(source_tex.Sample(linear_sampler, i.uv).rgb, 1.0);
}
)";

com_ptr<ID3DBlob> compile(const char* entry, const char* target) {
  com_ptr<ID3DBlob> code;
  com_ptr<ID3DBlob> errors;
  HRESULT hr = D3DCompile(kShaderSource, sizeof(kShaderSource) - 1, "sr_convert", nullptr, nullptr, entry, target,
                          D3DCOMPILE_OPTIMIZATION_LEVEL3, 0, code.put(), errors.put());
  if (FAILED(hr)) {
    std::string detail = errors ? std::string(static_cast<const char*>(errors->GetBufferPointer()),
                                              errors->GetBufferSize())
                                : std::string();
    throw HrError(hr, "D3DCompile(" + std::string(entry) + ") failed: " + hr_to_string(hr) + " " + detail);
  }
  return code;
}

RECT clamp_rect(RECT r, UINT width, UINT height) {
  r.left = std::clamp<LONG>(r.left, 0, static_cast<LONG>(width));
  r.top = std::clamp<LONG>(r.top, 0, static_cast<LONG>(height));
  r.right = std::clamp<LONG>(r.right, r.left, static_cast<LONG>(width));
  r.bottom = std::clamp<LONG>(r.bottom, r.top, static_cast<LONG>(height));
  if (r.right - r.left < 2 || r.bottom - r.top < 2) r = RECT{0, 0, static_cast<LONG>(width), static_cast<LONG>(height)};
  return r;
}

}  // namespace

RECT letterbox_rect(int src_w, int src_h, int dst_w, int dst_h) {
  if (src_w <= 0 || src_h <= 0) return RECT{0, 0, dst_w, dst_h};
  // Integer arithmetic: compare src_w/src_h with dst_w/dst_h without rounding drift.
  int64_t w, h;
  if (static_cast<int64_t>(src_w) * dst_h >= static_cast<int64_t>(dst_w) * src_h) {
    w = dst_w;
    h = static_cast<int64_t>(src_h) * dst_w / src_w;
  } else {
    h = dst_h;
    w = static_cast<int64_t>(src_w) * dst_h / src_h;
  }
  w = std::max<int64_t>(2, w & ~int64_t(1));
  h = std::max<int64_t>(2, h & ~int64_t(1));
  LONG x = static_cast<LONG>(((dst_w - w) / 2) & ~int64_t(1));
  LONG y = static_cast<LONG>(((dst_h - h) / 2) & ~int64_t(1));
  return RECT{x, y, x + static_cast<LONG>(w), y + static_cast<LONG>(h)};
}

FrameConverter::FrameConverter(std::shared_ptr<D3DDevice> device, UINT out_width, UINT out_height, UINT fps,
                               ConverterKind kind)
    : device_(std::move(device)), out_width_(out_width), out_height_(out_height), fps_(fps), kind_(kind) {
  if (kind_ == ConverterKind::VideoProcessor) {
    if (!device_->has_video()) throw HrError(E_NOINTERFACE, "Device has no D3D11 video support");
    // Validate up front so the encoder can fall back before opening the sink writer.
    std::lock_guard<std::recursive_mutex> guard(device_->lock());
    ensure_video_processor(out_width_, out_height_);
  } else {
    std::lock_guard<std::recursive_mutex> guard(device_->lock());
    ensure_shader();
  }
}

void FrameConverter::convert(ID3D11Texture2D* src, RECT src_rect, ID3D11Texture2D* dst) {
  D3D11_TEXTURE2D_DESC desc{};
  src->GetDesc(&desc);
  src_rect = clamp_rect(src_rect, desc.Width, desc.Height);
  if (kind_ == ConverterKind::VideoProcessor) {
    convert_video_processor(src, src_rect, dst);
  } else {
    convert_shader(src, src_rect, dst);
  }
}

void FrameConverter::ensure_video_processor(UINT in_width, UINT in_height) {
  if (vp_ && in_width == vp_in_width_ && in_height == vp_in_height_) return;
  vp_ = nullptr;
  vp_enum_ = nullptr;
  vp_inputs_.clear();
  vp_outputs_.clear();

  D3D11_VIDEO_PROCESSOR_CONTENT_DESC content{};
  content.InputFrameFormat = D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE;
  content.InputFrameRate = DXGI_RATIONAL{fps_, 1};
  content.InputWidth = in_width;
  content.InputHeight = in_height;
  content.OutputFrameRate = DXGI_RATIONAL{fps_, 1};
  content.OutputWidth = out_width_;
  content.OutputHeight = out_height_;
  content.Usage = D3D11_VIDEO_USAGE_OPTIMAL_SPEED;
  check(device_->video_device()->CreateVideoProcessorEnumerator(&content, vp_enum_.put()),
        "CreateVideoProcessorEnumerator");

  UINT flags = 0;
  check(vp_enum_->CheckVideoProcessorFormat(DXGI_FORMAT_B8G8R8A8_UNORM, &flags), "CheckVideoProcessorFormat(BGRA)");
  if ((flags & D3D11_VIDEO_PROCESSOR_FORMAT_SUPPORT_INPUT) == 0)
    throw HrError(E_NOTIMPL, "Video processor cannot read BGRA input");
  flags = 0;
  check(vp_enum_->CheckVideoProcessorFormat(DXGI_FORMAT_NV12, &flags), "CheckVideoProcessorFormat(NV12)");
  if ((flags & D3D11_VIDEO_PROCESSOR_FORMAT_SUPPORT_OUTPUT) == 0)
    throw HrError(E_NOTIMPL, "Video processor cannot write NV12 output");

  check(device_->video_device()->CreateVideoProcessor(vp_enum_.get(), 0, vp_.put()), "CreateVideoProcessor");

  auto* vc = device_->video_context();
  // Full-range RGB in, BT.709 limited-range YCbCr out: the standard H.264 signalling that
  // PowerPoint / Windows decoders assume.
  D3D11_VIDEO_PROCESSOR_COLOR_SPACE in_cs{};
  in_cs.RGB_Range = 0;
  in_cs.YCbCr_Matrix = 1;
  in_cs.Nominal_Range = D3D11_VIDEO_PROCESSOR_NOMINAL_RANGE_0_255;
  vc->VideoProcessorSetStreamColorSpace(vp_.get(), 0, &in_cs);
  D3D11_VIDEO_PROCESSOR_COLOR_SPACE out_cs{};
  out_cs.RGB_Range = 0;
  out_cs.YCbCr_Matrix = 1;
  out_cs.Nominal_Range = D3D11_VIDEO_PROCESSOR_NOMINAL_RANGE_16_235;
  vc->VideoProcessorSetOutputColorSpace(vp_.get(), &out_cs);
  vc->VideoProcessorSetStreamFrameFormat(vp_.get(), 0, D3D11_VIDEO_FRAME_FORMAT_PROGRESSIVE);
  vc->VideoProcessorSetStreamAutoProcessingMode(vp_.get(), 0, FALSE);
  D3D11_VIDEO_COLOR black{};
  black.RGBA.R = 0.0f;
  black.RGBA.G = 0.0f;
  black.RGBA.B = 0.0f;
  black.RGBA.A = 1.0f;
  vc->VideoProcessorSetOutputBackgroundColor(vp_.get(), FALSE, &black);

  vp_in_width_ = in_width;
  vp_in_height_ = in_height;
}

void FrameConverter::convert_video_processor(ID3D11Texture2D* src, const RECT& src_rect, ID3D11Texture2D* dst) {
  D3D11_TEXTURE2D_DESC desc{};
  src->GetDesc(&desc);
  ensure_video_processor(desc.Width, desc.Height);

  ID3D11VideoProcessorInputView* input = find_cached(vp_inputs_, src);
  if (input == nullptr) {
    D3D11_VIDEO_PROCESSOR_INPUT_VIEW_DESC vd{};
    vd.FourCC = 0;
    vd.ViewDimension = D3D11_VPIV_DIMENSION_TEXTURE2D;
    vd.Texture2D.MipSlice = 0;
    vd.Texture2D.ArraySlice = 0;
    com_ptr<ID3D11VideoProcessorInputView> view;
    check(device_->video_device()->CreateVideoProcessorInputView(src, vp_enum_.get(), &vd, view.put()),
          "CreateVideoProcessorInputView");
    input = view.get();
    store_cached(vp_inputs_, src, std::move(view));
  }
  ID3D11VideoProcessorOutputView* output = find_cached(vp_outputs_, dst);
  if (output == nullptr) {
    D3D11_VIDEO_PROCESSOR_OUTPUT_VIEW_DESC vd{};
    vd.ViewDimension = D3D11_VPOV_DIMENSION_TEXTURE2D;
    vd.Texture2D.MipSlice = 0;
    com_ptr<ID3D11VideoProcessorOutputView> view;
    check(device_->video_device()->CreateVideoProcessorOutputView(dst, vp_enum_.get(), &vd, view.put()),
          "CreateVideoProcessorOutputView");
    output = view.get();
    store_cached(vp_outputs_, dst, std::move(view));
  }

  auto* vc = device_->video_context();
  RECT dest = letterbox_rect(src_rect.right - src_rect.left, src_rect.bottom - src_rect.top,
                             static_cast<int>(out_width_), static_cast<int>(out_height_));
  RECT target{0, 0, static_cast<LONG>(out_width_), static_cast<LONG>(out_height_)};
  vc->VideoProcessorSetStreamSourceRect(vp_.get(), 0, TRUE, &src_rect);
  vc->VideoProcessorSetStreamDestRect(vp_.get(), 0, TRUE, &dest);
  vc->VideoProcessorSetOutputTargetRect(vp_.get(), TRUE, &target);

  D3D11_VIDEO_PROCESSOR_STREAM stream{};
  stream.Enable = TRUE;
  stream.pInputSurface = input;
  check(vc->VideoProcessorBlt(vp_.get(), output, 0, 1, &stream), "VideoProcessorBlt");
}

void FrameConverter::ensure_shader() {
  if (vs_) return;
  auto* dev = device_->device();
  auto vs_code = compile("vs_main", "vs_4_0");
  auto ps_code = compile("ps_main", "ps_4_0");
  check(dev->CreateVertexShader(vs_code->GetBufferPointer(), vs_code->GetBufferSize(), nullptr, vs_.put()),
        "CreateVertexShader");
  check(dev->CreatePixelShader(ps_code->GetBufferPointer(), ps_code->GetBufferSize(), nullptr, ps_.put()),
        "CreatePixelShader");

  D3D11_SAMPLER_DESC sd{};
  sd.Filter = D3D11_FILTER_MIN_MAG_MIP_LINEAR;
  sd.AddressU = D3D11_TEXTURE_ADDRESS_CLAMP;
  sd.AddressV = D3D11_TEXTURE_ADDRESS_CLAMP;
  sd.AddressW = D3D11_TEXTURE_ADDRESS_CLAMP;
  sd.ComparisonFunc = D3D11_COMPARISON_NEVER;
  sd.MaxLOD = D3D11_FLOAT32_MAX;
  check(dev->CreateSamplerState(&sd, sampler_.put()), "CreateSamplerState");

  D3D11_BUFFER_DESC bd{};
  bd.ByteWidth = 16;
  bd.Usage = D3D11_USAGE_DEFAULT;
  bd.BindFlags = D3D11_BIND_CONSTANT_BUFFER;
  check(dev->CreateBuffer(&bd, nullptr, constants_.put()), "CreateBuffer(constants)");
}

void FrameConverter::convert_shader(ID3D11Texture2D* src, const RECT& src_rect, ID3D11Texture2D* dst) {
  ensure_shader();
  auto* dev = device_->device();
  auto* ctx = device_->context();

  ID3D11ShaderResourceView* srv = find_cached(srvs_, src);
  if (srv == nullptr) {
    com_ptr<ID3D11ShaderResourceView> view;
    check(dev->CreateShaderResourceView(src, nullptr, view.put()), "CreateShaderResourceView");
    srv = view.get();
    store_cached(srvs_, src, std::move(view));
  }
  ID3D11RenderTargetView* rtv = find_cached(rtvs_, dst);
  if (rtv == nullptr) {
    com_ptr<ID3D11RenderTargetView> view;
    check(dev->CreateRenderTargetView(dst, nullptr, view.put()), "CreateRenderTargetView");
    rtv = view.get();
    store_cached(rtvs_, dst, std::move(view));
  }

  D3D11_TEXTURE2D_DESC desc{};
  src->GetDesc(&desc);
  const float uv[4] = {
      static_cast<float>(src_rect.left) / desc.Width, static_cast<float>(src_rect.top) / desc.Height,
      static_cast<float>(src_rect.right) / desc.Width, static_cast<float>(src_rect.bottom) / desc.Height};
  ctx->UpdateSubresource(constants_.get(), 0, nullptr, uv, 0, 0);

  const float black[4] = {0.0f, 0.0f, 0.0f, 1.0f};
  ctx->ClearRenderTargetView(rtv, black);

  RECT dest = letterbox_rect(src_rect.right - src_rect.left, src_rect.bottom - src_rect.top,
                             static_cast<int>(out_width_), static_cast<int>(out_height_));
  D3D11_VIEWPORT vp{};
  vp.TopLeftX = static_cast<float>(dest.left);
  vp.TopLeftY = static_cast<float>(dest.top);
  vp.Width = static_cast<float>(dest.right - dest.left);
  vp.Height = static_cast<float>(dest.bottom - dest.top);
  vp.MaxDepth = 1.0f;

  ID3D11Buffer* cb = constants_.get();
  ID3D11SamplerState* sampler = sampler_.get();
  ctx->IASetInputLayout(nullptr);
  ctx->IASetPrimitiveTopology(D3D11_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
  ctx->VSSetShader(vs_.get(), nullptr, 0);
  ctx->VSSetConstantBuffers(0, 1, &cb);
  ctx->PSSetShader(ps_.get(), nullptr, 0);
  ctx->PSSetShaderResources(0, 1, &srv);
  ctx->PSSetSamplers(0, 1, &sampler);
  ctx->RSSetState(nullptr);
  ctx->RSSetViewports(1, &vp);
  ctx->OMSetBlendState(nullptr, nullptr, 0xffffffff);
  ctx->OMSetRenderTargets(1, &rtv, nullptr);
  ctx->Draw(3, 0);

  ID3D11ShaderResourceView* null_srv = nullptr;
  ctx->PSSetShaderResources(0, 1, &null_srv);
  ctx->OMSetRenderTargets(0, nullptr, nullptr);
}

}  // namespace sr
