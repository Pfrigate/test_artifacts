// pybind11 bindings for the _srnative extension. Every potentially long native call
// releases the GIL so the PySide6 event loop never blocks on capture or encoding.
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include "d3d_device.h"
#include "frame_converter.h"
#include "mf_caps.h"
#include "mf_encoder.h"
#include "wgc_capture.h"

namespace py = pybind11;
using namespace sr;

namespace {

PyObject* g_native_error = nullptr;

void raise_native_error(const std::string& message, HRESULT hr) {
  PyObject* args = Py_BuildValue("(sK)", message.c_str(), static_cast<unsigned long long>(static_cast<uint32_t>(hr)));
  PyErr_SetObject(g_native_error, args);
  Py_XDECREF(args);
}

py::dict encoder_info_to_dict(const EncoderInfo& info) {
  py::dict d;
  d["name"] = utf8(info.name);
  d["clsid"] = utf8(info.clsid);
  d["hardware"] = info.hardware;
  d["async"] = info.async;
  d["vendor_id"] = utf8(info.vendor_id);
  py::list modes;
  for (const auto& m : info.modes) {
    py::dict md;
    md["width"] = m.width;
    md["height"] = m.height;
    md["fps"] = m.fps;
    md["supported"] = m.supported;
    modes.append(md);
  }
  d["modes"] = modes;
  d["error"] = info.error;
  return d;
}

py::dict pipeline_stats_to_dict(const PipelineStats& s) {
  py::dict d;
  d["state"] = s.state;
  d["error_kind"] = s.error_kind;
  d["error"] = s.error;
  d["frames_written"] = s.frames_written;
  d["frames_captured"] = s.frames_captured;
  d["frames_duplicated"] = s.frames_duplicated;
  d["frames_dropped"] = s.frames_dropped;
  d["avg_tick_ms"] = s.avg_tick_ms;
  d["max_tick_ms"] = s.max_tick_ms;
  d["recorded_seconds"] = s.recorded_seconds;
  d["source_width"] = s.source_width;
  d["source_height"] = s.source_height;
  return d;
}

std::optional<RECT> to_rect(const std::optional<std::tuple<int, int, int, int>>& crop) {
  if (!crop) return std::nullopt;
  const auto& [x, y, w, h] = *crop;
  return RECT{x, y, x + w, y + h};
}

}  // namespace

PYBIND11_MODULE(_srnative, m) {
  m.doc() = "ScreenRecorder native layer: Direct3D 11, Windows Graphics Capture, Media Foundation";
  m.attr("__version__") = "1.0.0";

  g_native_error = PyErr_NewException("screenrecorder._srnative.NativeError", PyExc_RuntimeError, nullptr);
  m.attr("NativeError") = py::handle(g_native_error);
  py::register_exception_translator([](std::exception_ptr p) {
    try {
      if (p) std::rethrow_exception(p);
    } catch (const HrError& e) {
      raise_native_error(e.what(), e.hr());
    } catch (const winrt::hresult_error& e) {
      raise_native_error(utf8(std::wstring(e.message())), e.code());
    }
  });

  m.def("wgc_supported", &wgc_is_supported, "Windows.Graphics.Capture availability");

  m.def(
      "enumerate_adapters",
      [] {
        py::list result;
        for (const auto& a : enumerate_adapters()) {
          py::dict d;
          d["index"] = a.index;
          d["description"] = utf8(a.description);
          d["vendor_id"] = a.vendor_id;
          d["dedicated_video_memory"] = a.dedicated_video_memory;
          d["software"] = a.software;
          py::list outputs;
          for (const auto& o : a.outputs) {
            py::dict od;
            od["device_name"] = utf8(o.device_name);
            od["hmonitor"] = o.hmonitor;
            od["rect"] = py::make_tuple(o.desktop.left, o.desktop.top, o.desktop.right - o.desktop.left,
                                        o.desktop.bottom - o.desktop.top);
            outputs.append(od);
          }
          d["outputs"] = outputs;
          result.append(d);
        }
        return result;
      },
      "DXGI adapters and their outputs (HMONITOR, desktop rect)");

  m.def(
      "enumerate_encoders",
      [](bool test_modes) {
        std::vector<EncoderInfo> encoders;
        {
          py::gil_scoped_release release;
          encoders = enumerate_h264_encoders(test_modes);
        }
        py::list result;
        for (const auto& e : encoders) result.append(encoder_info_to_dict(e));
        return result;
      },
      py::arg("test_modes") = true, "H.264 encoder MFTs available on this machine");

  m.def(
      "probe",
      [](const std::wstring& path, bool count_frames) {
        ProbeResult r;
        {
          py::gil_scoped_release release;
          r = probe_file(path, count_frames);
        }
        py::dict d;
        d["codec"] = r.codec;
        d["width"] = r.width;
        d["height"] = r.height;
        d["fps_num"] = r.fps_num;
        d["fps_den"] = r.fps_den;
        d["fps"] = r.fps_den ? static_cast<double>(r.fps_num) / r.fps_den : 0.0;
        d["profile"] = r.profile;
        d["duration"] = static_cast<double>(r.duration_hns) / 1e7;
        d["frame_count"] = r.frame_count;
        d["keyframe_count"] = r.keyframe_count;
        d["first_pts"] = static_cast<double>(r.first_pts_hns) / 1e7;
        d["last_pts"] = static_cast<double>(r.last_pts_hns) / 1e7;
        d["timestamps_unique"] = r.timestamps_unique;
        d["has_audio"] = r.has_audio;
        return d;
      },
      py::arg("path"), py::arg("count_frames") = true, "Inspect an MP4 with the Media Foundation Source Reader");

  m.def(
      "thumbnail",
      [](const std::wstring& path, double position_seconds) {
        std::tuple<std::string, UINT, UINT> result;
        {
          py::gil_scoped_release release;
          result = decode_thumbnail(path, static_cast<int64_t>(position_seconds * 1e7));
        }
        return py::make_tuple(py::bytes(std::get<0>(result)), std::get<1>(result), std::get<2>(result));
      },
      py::arg("path"), py::arg("position_seconds") = 0.5, "Decode one frame to BGRA bytes: (bytes, width, height)");

  m.def(
      "letterbox",
      [](int src_w, int src_h, int dst_w, int dst_h) {
        RECT r = letterbox_rect(src_w, src_h, dst_w, dst_h);
        return py::make_tuple(r.left, r.top, r.right - r.left, r.bottom - r.top);
      },
      "Letterbox rectangle used by the GPU converter: (x, y, w, h)");

  py::class_<D3DDevice, std::shared_ptr<D3DDevice>>(m, "Device")
      .def(py::init([](int adapter_index, bool allow_warp) {
             py::gil_scoped_release release;
             return std::make_shared<D3DDevice>(adapter_index, allow_warp);
           }),
           py::arg("adapter_index") = -1, py::arg("allow_warp") = true)
      .def_property_readonly("adapter_name", [](const D3DDevice& d) { return utf8(d.adapter_name()); })
      .def_property_readonly("vendor_id", &D3DDevice::vendor_id)
      .def_property_readonly("is_hardware", &D3DDevice::is_hardware)
      .def_property_readonly("is_warp", &D3DDevice::is_warp)
      .def_property_readonly("feature_level", &D3DDevice::feature_level)
      .def_property_readonly("video_processor_available", &D3DDevice::has_video)
      .def("removed_reason", [](const D3DDevice& d) { return static_cast<int64_t>(static_cast<uint32_t>(d.removed_reason())); });

  py::class_<VideoEncoder, std::shared_ptr<VideoEncoder>>(m, "Encoder")
      .def(py::init([](std::shared_ptr<D3DDevice> device, const std::wstring& path, UINT width, UINT height, UINT fps,
                       UINT bitrate, bool prefer_hardware, const std::string& profile, UINT gop_seconds,
                       const std::string& converter, UINT pool_size) {
             EncoderConfig cfg;
             cfg.path = path;
             cfg.width = width;
             cfg.height = height;
             cfg.fps = fps;
             cfg.bitrate = bitrate;
             cfg.prefer_hardware = prefer_hardware;
             cfg.profile = profile;
             cfg.gop_seconds = gop_seconds;
             cfg.converter = converter;
             cfg.pool_size = pool_size;
             py::gil_scoped_release release;
             return std::make_shared<VideoEncoder>(std::move(device), std::move(cfg));
           }),
           py::arg("device"), py::arg("path"), py::arg("width"), py::arg("height"), py::arg("fps"),
           py::arg("bitrate"), py::arg("prefer_hardware") = true, py::arg("profile") = "high",
           py::arg("gop_seconds") = 2, py::arg("converter") = "auto", py::arg("pool_size") = 6)
      .def(
          "write_bgra",
          [](VideoEncoder& e, py::buffer frame, int64_t index) {
            py::buffer_info info = frame.request();
            if (info.ndim != 3 || info.shape[2] != 4 || info.itemsize != 1)
              throw py::value_error("frame must be an HxWx4 uint8 BGRA array");
            if (info.strides[2] != 1 || info.strides[1] != 4 || info.strides[0] < info.shape[1] * 4)
              throw py::value_error("frame rows must be contiguous BGRA pixels");
            const auto* data = static_cast<const uint8_t*>(info.ptr);
            const UINT h = static_cast<UINT>(info.shape[0]);
            const UINT w = static_cast<UINT>(info.shape[1]);
            const UINT pitch = static_cast<UINT>(info.strides[0]);
            py::gil_scoped_release release;
            return e.write_bgra(data, w, h, pitch, index);
          },
          py::arg("frame"), py::arg("index") = -1,
          "Upload a BGRA frame (row stride may exceed width*4) and encode it as frame `index` "
          "(default: next frame)")
      .def("write_synthetic", &VideoEncoder::write_synthetic, py::call_guard<py::gil_scoped_release>())
      .def("finalize", &VideoEncoder::finalize, py::call_guard<py::gil_scoped_release>())
      .def_property_readonly("path", [](const VideoEncoder& e) { return e.config().path; })
      .def_property_readonly("width", [](const VideoEncoder& e) { return e.config().width; })
      .def_property_readonly("height", [](const VideoEncoder& e) { return e.config().height; })
      .def_property_readonly("fps", [](const VideoEncoder& e) { return e.config().fps; })
      .def_property_readonly("bitrate", [](const VideoEncoder& e) { return e.config().bitrate; })
      .def_property_readonly("hardware", &VideoEncoder::hardware)
      .def_property_readonly("encoder_name", [](const VideoEncoder& e) { return utf8(e.encoder_name()); })
      .def_property_readonly("converter", &VideoEncoder::converter_name)
      .def_property_readonly("input_subtype", &VideoEncoder::input_subtype)
      .def_property_readonly("profile", &VideoEncoder::profile_used)
      .def_property_readonly("finalized", &VideoEncoder::finalized)
      .def_property_readonly("frames_written", [](const VideoEncoder& e) { return e.stats().frames_written; })
      .def_property_readonly("last_index", &VideoEncoder::last_index)
      .def("stats", [](const VideoEncoder& e) {
        const EncoderStats s = e.stats();
        const double n = s.frames_written > 0 ? static_cast<double>(s.frames_written) : 1.0;
        py::dict d;
        d["frames_written"] = s.frames_written;
        d["last_index"] = s.last_index;
        d["avg_convert_ms"] = s.total_convert_ms / n;
        d["avg_write_ms"] = s.total_write_ms / n;
        d["avg_wait_ms"] = s.total_wait_ms / n;
        d["max_frame_ms"] = s.max_write_ms;
        return d;
      });

  py::class_<CapturePipeline, std::shared_ptr<CapturePipeline>>(m, "CapturePipeline")
      .def(py::init([](std::shared_ptr<D3DDevice> device, std::shared_ptr<VideoEncoder> encoder,
                       const std::string& kind, uint64_t handle,
                       std::optional<std::tuple<int, int, int, int>> crop, bool cursor, bool border,
                       std::optional<std::tuple<int, int>> synthetic_size, int64_t max_frames) {
             CaptureTarget target;
             if (kind == "monitor") {
               target.kind = SourceKind::Monitor;
             } else if (kind == "window") {
               target.kind = SourceKind::Window;
             } else if (kind == "synthetic") {
               target.kind = SourceKind::Synthetic;
               if (synthetic_size) {
                 target.synthetic_width = static_cast<UINT>(std::get<0>(*synthetic_size));
                 target.synthetic_height = static_cast<UINT>(std::get<1>(*synthetic_size));
               } else {
                 target.synthetic_width = encoder->config().width;
                 target.synthetic_height = encoder->config().height;
               }
             } else {
               throw py::value_error("kind must be 'monitor', 'window' or 'synthetic'");
             }
             target.handle = handle;
             return std::make_shared<CapturePipeline>(std::move(device), std::move(encoder), target, to_rect(crop),
                                                      cursor, border, max_frames);
           }),
           py::arg("device"), py::arg("encoder"), py::arg("kind"), py::arg("handle") = 0, py::arg("crop") = py::none(),
           py::arg("cursor") = true, py::arg("border") = false, py::arg("synthetic_size") = py::none(),
           py::arg("max_frames") = 0)
      .def("start", &CapturePipeline::start, py::call_guard<py::gil_scoped_release>())
      .def("pause", &CapturePipeline::pause, py::call_guard<py::gil_scoped_release>())
      .def("resume", &CapturePipeline::resume, py::call_guard<py::gil_scoped_release>())
      .def("stop", &CapturePipeline::stop, py::call_guard<py::gil_scoped_release>())
      .def("stats", [](const CapturePipeline& p) { return pipeline_stats_to_dict(p.stats()); });
}
