// Direct3D 11 device shared by capture, conversion and the Media Foundation encoder.
#pragma once

#include "com_util.h"

#include <d3d11_4.h>
#include <dxgi1_6.h>

#include <mutex>
#include <string>
#include <vector>

#include <winrt/Windows.Graphics.DirectX.Direct3D11.h>

namespace sr {

struct OutputInfo {
  std::wstring device_name;
  uint64_t hmonitor = 0;
  RECT desktop{};
};

struct AdapterInfo {
  int index = 0;
  std::wstring description;
  uint32_t vendor_id = 0;
  uint64_t dedicated_video_memory = 0;
  bool software = false;
  std::vector<OutputInfo> outputs;
};

std::vector<AdapterInfo> enumerate_adapters();

class D3DDevice {
 public:
  explicit D3DDevice(int adapter_index = -1, bool allow_warp = true);

  ID3D11Device* device() const { return device_.get(); }
  ID3D11DeviceContext* context() const { return context_.get(); }
  ID3D11VideoDevice* video_device() const { return video_device_.get(); }
  ID3D11VideoContext* video_context() const { return video_context_.get(); }
  bool has_video() const { return video_device_ && video_context_; }

  const std::wstring& adapter_name() const { return adapter_name_; }
  uint32_t vendor_id() const { return vendor_id_; }
  bool is_warp() const { return warp_; }
  bool is_hardware() const { return !warp_ && vendor_id_ != 0x1414; }
  std::string feature_level() const;

  HRESULT removed_reason() const { return device_->GetDeviceRemovedReason(); }

  winrt::Windows::Graphics::DirectX::Direct3D11::IDirect3DDevice winrt_device();

  // The runtime is multithread-protected, but sequences of context calls
  // (set state + draw/blt) issued from different threads must not interleave.
  std::recursive_mutex& lock() { return lock_; }

  com_ptr<ID3D11Texture2D> create_texture(UINT width, UINT height, DXGI_FORMAT format, UINT bind_flags) const;

 private:
  com_ptr<ID3D11Device> device_;
  com_ptr<ID3D11DeviceContext> context_;
  com_ptr<ID3D11VideoDevice> video_device_;
  com_ptr<ID3D11VideoContext> video_context_;
  winrt::Windows::Graphics::DirectX::Direct3D11::IDirect3DDevice winrt_device_{nullptr};
  D3D_FEATURE_LEVEL feature_level_ = D3D_FEATURE_LEVEL_10_0;
  std::wstring adapter_name_;
  uint32_t vendor_id_ = 0;
  bool warp_ = false;
  std::recursive_mutex lock_;
};

}  // namespace sr
