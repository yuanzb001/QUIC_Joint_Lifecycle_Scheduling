#include <pybind11/pybind11.h>
#include <pybind11/functional.h>
#include "api/quic_media_client.hpp"
#include "api/quic_media_server.hpp"

namespace py = pybind11;

PYBIND11_MODULE(quic_media, m) {
    m.doc() = "Python bindings for QUIC Media Client"; // optional module docstring

    py::class_<moq::transport::AckEvent>(m, "AckEvent")
        .def_readonly("stream_id", &moq::transport::AckEvent::stream_id)
        .def_readonly("frame_id", &moq::transport::AckEvent::frame_id)
        .def_readonly("fragment_idx", &moq::transport::AckEvent::fragment_idx)
        .def_readonly("canceled", &moq::transport::AckEvent::canceled)
        .def_readonly("delay_us", &moq::transport::AckEvent::delay_us);

    py::class_<moq::transport::NetworkStats>(m, "NetworkStats")
        .def_readonly("rtt_us", &moq::transport::NetworkStats::rtt_us)
        .def_readonly("min_rtt_us", &moq::transport::NetworkStats::min_rtt_us)
        .def_readonly("max_rtt_us", &moq::transport::NetworkStats::max_rtt_us)
        .def_readonly("rtt_variance_us", &moq::transport::NetworkStats::rtt_variance_us)
        .def_readonly("send_path_mtu", &moq::transport::NetworkStats::send_path_mtu)
        .def_readonly("cwnd_bytes", &moq::transport::NetworkStats::cwnd_bytes)
        .def_readonly("bytes_sent", &moq::transport::NetworkStats::bytes_sent)
        .def_readonly("bytes_received", &moq::transport::NetworkStats::bytes_received)
        .def_readonly("send_total_stream_bytes", &moq::transport::NetworkStats::send_total_stream_bytes)
        .def_readonly("recv_total_stream_bytes", &moq::transport::NetworkStats::recv_total_stream_bytes)
        .def_readonly("packets_sent", &moq::transport::NetworkStats::packets_sent)
        .def_readonly("packets_lost", &moq::transport::NetworkStats::packets_lost)
        .def_readonly("send_spurious_lost_packets", &moq::transport::NetworkStats::send_spurious_lost_packets)
        .def_readonly("send_congestion_count", &moq::transport::NetworkStats::send_congestion_count)
        .def_readonly("estimated_bandwidth_bps", &moq::transport::NetworkStats::estimated_bandwidth_bps)
        .def_readonly("bytes_in_flight", &moq::transport::NetworkStats::bytes_in_flight)
        .def_readonly("posted_bytes", &moq::transport::NetworkStats::posted_bytes)
        .def_readonly("ideal_bytes", &moq::transport::NetworkStats::ideal_bytes);

    py::class_<QuicMediaClient>(m, "QuicMediaClient")
        .def(py::init<>()) // Assuming default constructor exists
        
        // Configuration
        .def("configure_mtu", &QuicMediaClient::configure_mtu, 
             py::arg("min_mtu"), py::arg("max_mtu"))
        .def("set_payload_size", &QuicMediaClient::set_payload_size,
             py::arg("payload_size"))
        .def("set_stream_scheduling_scheme", &QuicMediaClient::set_stream_scheduling_scheme,
             py::arg("scheme"))
             
        // Connection
        .def("connect", &QuicMediaClient::connect,
             py::call_guard<py::gil_scoped_release>(),
             py::arg("host"), py::arg("port"))
             
        // Stream lifecycle
        .def("open_stream", &QuicMediaClient::open_stream,
             py::call_guard<py::gil_scoped_release>())
        .def("close_stream", &QuicMediaClient::close_stream,
             py::call_guard<py::gil_scoped_release>(),
             py::arg("stream_id"))
        .def("drop_stream", &QuicMediaClient::drop_stream,
             py::call_guard<py::gil_scoped_release>(),
             py::arg("stream_id"))
        .def("set_stream_priority", &QuicMediaClient::set_stream_priority,
             py::arg("stream_id"), py::arg("priority"))
        .def("disconnect", &QuicMediaClient::disconnect,
             py::call_guard<py::gil_scoped_release>(),
             py::arg("force") = false)
        .def("get_network_stats", &QuicMediaClient::get_network_stats,
             py::call_guard<py::gil_scoped_release>())
        .def("set_ack_callback", [](QuicMediaClient& self, std::function<void(const moq::transport::AckEvent&)> cb) {
            self.set_ack_callback([cb](const moq::transport::AckEvent& event) {
                py::gil_scoped_acquire acquire;
                cb(event);
            });
        })
             
        // Media object input
        .def("enqueue_object", 
            [](QuicMediaClient& self, uint32_t stream_id, uint32_t frame_id, uint16_t subpic_id, uint16_t object_type, py::buffer data, uint16_t priority, bool is_fin) {
                py::buffer_info info = data.request();
                
                // Release GIL before calling C++ sending logic
                py::gil_scoped_release release;
                return self.enqueue_object(stream_id, frame_id, subpic_id, object_type, static_cast<const uint8_t*>(info.ptr), info.size * info.itemsize, priority, is_fin);
            },
            py::arg("stream_id"), py::arg("frame_id"), py::arg("subpic_id"), py::arg("object_type"), py::arg("data"), py::arg("priority") = 0, py::arg("is_fin") = false);

    py::class_<QuicMediaServer>(m, "QuicMediaServer")
        .def(py::init<>())
        .def("configure_mtu", &QuicMediaServer::configure_mtu,
             py::arg("min_mtu"), py::arg("max_mtu"))
        .def("start_server", &QuicMediaServer::start_server,
             py::call_guard<py::gil_scoped_release>(),
             py::arg("host"), py::arg("port"), py::arg("cert_file"), py::arg("key_file"))
        .def("run", &QuicMediaServer::run,
             py::call_guard<py::gil_scoped_release>())
        .def("stop_server", &QuicMediaServer::stop_server,
             py::call_guard<py::gil_scoped_release>())
        .def("set_recv_callback", [](QuicMediaServer& self, py::object cb) {
            if (cb.is_none()) {
                self.set_recv_callback(nullptr);
            } else {
                self.set_recv_callback([cb](uint32_t stream_id, const uint8_t* data, size_t len) {
                    py::gil_scoped_acquire acquire;
                    cb(stream_id, py::bytes(reinterpret_cast<const char*>(data), len));
                });
            }
        });
}
