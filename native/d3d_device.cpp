#include "d3d_device.h"

#include <windows.graphics.directx.direct3d11.interop.h>

namespace sr {

std::vector<AdapterInfo> enumerate_adapters() {
  com_ptr<IDXGIFactory1> factory;
  check(CreateDXGIFactory1(IID_PPV_ARGS(factory.put())), "CreateDXGIFactory1");
  std::vector<AdapterInfo> result;
  for (UINT i = 0;; ++i) {
    com_ptr<IDXGIAdapter1> adapter;
    if (factory->EnumAdapters1(i, adapter.put()) == DXGI_ERROR_NOT_FOUND) break;
    DXGI_ADAPTER_DESC1 desc{};
    adapter->GetDesc1(&desc);
    AdapterInfo info;
    info.index = static_cast<int>(i);
    info.description = desc.Description;
    info.vendor_id = desc.VendorId;
    info.dedicated_video_memory = desc.DedicatedVideoMemory;
    info.software = (desc.Flags & DXGI_ADAPTER_FLAG_SOFTWARE) != 0;
    for (UINT j = 0;; ++j) {
      com_ptr<IDXGIOutput> output;
      if (adapter->EnumOutputs(j, output.put()) == DXGI_ERROR_NOT_FOUND) break;
      DXGI_OUTPUT_DESC od{};
      output->GetDesc(&od);
      OutputInfo out;
      out.device_name = od.DeviceName;
      out.hmonitor = reinterpret_cast<uint64_t>(od.Monitor);
      out.desktop = od.DesktopCoordinates;
      info.outputs.push_back(out);
    }
    result.push_back(std::move(info));
  }
  return result;
}

D3DDevice::D3DDevice(int adapter_index, bool allow_warp) {
  static const D3D_FEATURE_LEVEL levels[] = {D3D_FEATURE_LEVEL_11_1, D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_10_1,
                                             D3D_FEATURE_LEVEL_10_0};
  const UINT base_flags = D3D11_CREATE_DEVICE_BGRA_SUPPORT;

  com_ptr<IDXGIAdapter1> adapter;
  if (adapter_index >= 0) {
    com_ptr<IDXGIFactory1> factory;
    check(CreateDXGIFactory1(IID_PPV_ARGS(factory.put())), "CreateDXGIFactory1");
    check(factory->EnumAdapters1(static_cast<UINT>(adapter_index), adapter.put()), "EnumAdapters1");
  }

  auto create = [&](IDXGIAdapter* a, D3D_DRIVER_TYPE type, UINT flags) -> HRESULT {
    device_ = nullptr;
    context_ = nullptr;
    HRESULT hr = D3D11CreateDevice(a, type, nullptr, flags, levels, ARRAYSIZE(levels), D3D11_SDK_VERSION,
                                   device_.put(), &feature_level_, context_.put());
    if (hr == E_INVALIDARG) {
      // Runtimes without 11_1 reject the whole array; retry without it.
      device_ = nullptr;
      context_ = nullptr;
      hr = D3D11CreateDevice(a, type, nullptr, flags, levels + 1, ARRAYSIZE(levels) - 1, D3D11_SDK_VERSION,
                             device_.put(), &feature_level_, context_.put());
    }
    return hr;
  };

  const D3D_DRIVER_TYPE hw_type = adapter ? D3D_DRIVER_TYPE_UNKNOWN : D3D_DRIVER_TYPE_HARDWARE;
  HRESULT hr = create(adapter.get(), hw_type, base_flags | D3D11_CREATE_DEVICE_VIDEO_SUPPORT);
  if (FAILED(hr)) hr = create(adapter.get(), hw_type, base_flags);
  if (FAILED(hr) && allow_warp) {
    warp_ = true;
    hr = create(nullptr, D3D_DRIVER_TYPE_WARP, base_flags | D3D11_CREATE_DEVICE_VIDEO_SUPPORT);
    if (FAILED(hr)) hr = create(nullptr, D3D_DRIVER_TYPE_WARP, base_flags);
  }
  check(hr, "D3D11CreateDevice");

  if (auto mt = device_.try_as<ID3D11Multithread>()) mt->SetMultithreadProtected(TRUE);

  video_device_ = device_.try_as<ID3D11VideoDevice>();
  video_context_ = context_.try_as<ID3D11VideoContext>();

  auto dxgi_device = device_.as<IDXGIDevice>();
  com_ptr<IDXGIAdapter> used_adapter;
  if (SUCCEEDED(dxgi_device->GetAdapter(used_adapter.put()))) {
    DXGI_ADAPTER_DESC desc{};
    used_adapter->GetDesc(&desc);
    adapter_name_ = desc.Description;
    vendor_id_ = desc.VendorId;
  }
}

std::string D3DDevice::feature_level() const {
  switch (feature_level_) {
    case D3D_FEATURE_LEVEL_11_1: return "11_1";
    case D3D_FEATURE_LEVEL_11_0: return "11_0";
    case D3D_FEATURE_LEVEL_10_1: return "10_1";
    case D3D_FEATURE_LEVEL_10_0: return "10_0";
    default: return "unknown";
  }
}

winrt::Windows::Graphics::DirectX::Direct3D11::IDirect3DDevice D3DDevice::winrt_device() {
  if (!winrt_device_) {
    auto dxgi_device = device_.as<IDXGIDevice>();
    com_ptr<::IInspectable> inspectable;
    check(CreateDirect3D11DeviceFromDXGIDevice(dxgi_device.get(), inspectable.put()),
          "CreateDirect3D11DeviceFromDXGIDevice");
    winrt_device_ = inspectable.as<winrt::Windows::Graphics::DirectX::Direct3D11::IDirect3DDevice>();
  }
  return winrt_device_;
}

com_ptr<ID3D11Texture2D> D3DDevice::create_texture(UINT width, UINT height, DXGI_FORMAT format, UINT bind_flags) const {
  D3D11_TEXTURE2D_DESC desc{};
  desc.Width = width;
  desc.Height = height;
  desc.MipLevels = 1;
  desc.ArraySize = 1;
  desc.Format = format;
  desc.SampleDesc.Count = 1;
  desc.Usage = D3D11_USAGE_DEFAULT;
  desc.BindFlags = bind_flags;
  com_ptr<ID3D11Texture2D> texture;
  check(device_->CreateTexture2D(&desc, nullptr, texture.put()), "CreateTexture2D");
  return texture;
}

}  // namespace sr
