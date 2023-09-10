"""
This node locates Aruco AR markers in images and publishes their ids and poses.

Subscriptions:
   /camera/image_raw (sensor_msgs.msg.Image)
   /camera/camera_info (sensor_msgs.msg.CameraInfo)
   /camera/camera_info (sensor_msgs.msg.CameraInfo)

Published Topics:
    /aruco_poses (geometry_msgs.msg.PoseArray)
       Pose of all detected markers (suitable for rviz visualization)

    /aruco_markers (ros2_aruco_interfaces.msg.ArucoMarkers)
       Provides an array of all poses along with the corresponding
       marker ids.

Parameters:
    marker_size - size of the markers in meters (default .0625)
    aruco_dictionary_id - dictionary that was used to generate markers
                          (default DICT_5X5_250)
    image_topic - image topic to subscribe to (default /camera/image_raw)
    camera_info_topic - camera info topic to subscribe to
                         (default /camera/camera_info)

Author: Nathan Sprague
Version: 10/26/2020

"""

import rclpy
import rclpy.node
from rclpy.qos import qos_profile_sensor_data
from cv_bridge import CvBridge
import numpy as np
import cv2
import json
import tf_transformations
from sensor_msgs.msg import CameraInfo
from sensor_msgs.msg import Image
from geometry_msgs.msg import PoseArray, Pose
from std_srvs.srv import Trigger
from ros2_aruco_interfaces.msg import ArucoMarkers
from rcl_interfaces.msg import ParameterDescriptor, ParameterType

class ArucoNode(rclpy.node.Node):
    def __init__(self):
        super().__init__("aruco_node")

        # Declare and read parameters
        self.declare_parameter(
            name="marker_size",
            value=0.0625,
            descriptor=ParameterDescriptor(
                type=ParameterType.PARAMETER_DOUBLE,
                description="Size of the markers in meters.",
            ),
        )

        self.declare_parameter(
            name="aruco_dictionary_id",
            value="DICT_5X5_250",
            descriptor=ParameterDescriptor(
                type=ParameterType.PARAMETER_STRING,
                description="Dictionary that was used to generate markers.",
            ),
        )

        self.declare_parameter(
            name='marker_map',
            value={},
            descriptor=ParameterDescriptor(
                type=ParameterType.PARAMETER_STRING,
                description="Marker size map by marker ID.",
            ),
        )

        self.declare_parameter(
            name="image_topic",
            value="/camera/image_raw",
            descriptor=ParameterDescriptor(
                type=ParameterType.PARAMETER_STRING,
                description="Image topic to subscribe to.",
            ),
        )

        self.declare_parameter(
            name="camera_info_topic",
            value="/camera/camera_info",
            descriptor=ParameterDescriptor(
                type=ParameterType.PARAMETER_STRING,
                description="Camera info topic to subscribe to.",
            ),
        )

        self.declare_parameter(
            name="camera_frame",
            value="",
            descriptor=ParameterDescriptor(
                type=ParameterType.PARAMETER_STRING,
                description="Camera optical frame to use.",
            ),
        )

        self.marker_size = (
            self.get_parameter("marker_size").get_parameter_value().double_value
        )
        self.get_logger().info(f"Marker size: {self.marker_size}")

        dictionary_id_name = (
            self.get_parameter("aruco_dictionary_id").get_parameter_value().string_value
        )
        self.get_logger().info(f"Marker type: {dictionary_id_name}")

        self.marker_map = self.get_parameter('marker_map').value or {}
        """Map with custom sizes for markers by marker ID."""
        # Deserialize the marker_map parameter
        marker_map_str = self.get_parameter('marker_map').value
        try:
            marker_map_with_str_keys = json.loads(marker_map_str)
            # Convert string keys to integers
            self.marker_map = {int(k): v for k, v in marker_map_with_str_keys.items()}
            self.get_logger().info(
                f"Loaded marker map: {self.marker_map}")
        except json.JSONDecodeError:
            self.get_logger().error("Failed to parse marker_map parameter, using empty map.")
            self.marker_map = {}

        image_topic = (
            self.get_parameter("image_topic").get_parameter_value().string_value
        )
        self.get_logger().info(f"Image topic: {image_topic}")

        info_topic = (
            self.get_parameter("camera_info_topic").get_parameter_value().string_value
        )
        self.get_logger().info(f"Image info topic: {info_topic}")

        self.camera_frame = (
            self.get_parameter("camera_frame").get_parameter_value().string_value
        )

        # Make sure we have a valid dictionary id:
        try:
            dictionary_id = cv2.aruco.__getattribute__(dictionary_id_name)
            if type(dictionary_id) != type(cv2.aruco.DICT_5X5_100):
                raise AttributeError
        except AttributeError:
            self.get_logger().error(
                "bad aruco_dictionary_id: {}".format(dictionary_id_name)
            )
            options = "\n".join([s for s in dir(cv2.aruco) if s.startswith("DICT")])
            self.get_logger().error("valid options: {}".format(options))

        self.start_srv = self.create_service(
            Trigger,
            'aruco/start',
            self.start_cb)

        self.stop_srv = self.create_service(
            Trigger,
            'aruco/stop',
            self.stop_cb)

        # Default not active
        self.active = False

        # Set up subscriptions
        # self.info_sub = self.create_subscription(
        #     CameraInfo, info_topic, self.info_callback, qos_profile_sensor_data
        # )
        self.info_sub = self.create_subscription(
            CameraInfo, info_topic, self.info_callback, 10
        )

        # self.create_subscription(
        #     Image, image_topic, self.image_callback, qos_profile_sensor_data
        # )
        # self.img_sub = self.create_subscription(
        #     Image, image_topic, self.image_callback, 10
        # )

        self.img_sub = None

        # Set up publishers
        self.poses_pub = self.create_publisher(PoseArray, "aruco_poses", 10)
        self.markers_pub = self.create_publisher(ArucoMarkers, "aruco_markers", 10)

        # Set up fields for camera parameters
        self.info_msg = None
        self.intrinsic_mat = None
        self.distortion = None

        self.aruco_dictionary = cv2.aruco.getPredefinedDictionary(dictionary_id)
        self.aruco_parameters = cv2.aruco.DetectorParameters()
        self.bridge = CvBridge()

    def info_callback(self, info_msg):
        self.get_logger().info("INFO CB")
        self.info_msg = info_msg
        self.intrinsic_mat = np.reshape(np.array(self.info_msg.k), (3, 3))
        self.distortion = np.array(self.info_msg.d)
        # Assume that camera parameters will remain the same...
        self.destroy_subscription(self.info_sub)

    def image_callback(self, img_msg):
        # self.get_logger().info("IMG CB")
        # return

        if self.info_msg is None:
            self.get_logger().warn("No camera info has been received!")
            return

        cv_image = self.bridge.imgmsg_to_cv2(img_msg, desired_encoding="mono8")
        markers = ArucoMarkers()
        pose_array = PoseArray()
        if self.camera_frame == "":
            markers.header.frame_id = self.info_msg.header.frame_id
            pose_array.header.frame_id = self.info_msg.header.frame_id
        else:
            markers.header.frame_id = self.camera_frame
            pose_array.header.frame_id = self.camera_frame

        markers.header.stamp = img_msg.header.stamp
        pose_array.header.stamp = img_msg.header.stamp

        corners, marker_ids, rejected = cv2.aruco.detectMarkers(
            cv_image, self.aruco_dictionary, parameters=self.aruco_parameters
        )
        # self.get_logger().info(f"corners: {corners}")
        # self.get_logger().info(f"marker_ids: {marker_ids}")
        # self.get_logger().info(f"rejected: {rejected}")
        if marker_ids is not None:
            for i, marker_id in enumerate(marker_ids):
                specific_marker_size = self.get_marker_size(marker_id[0])

                if cv2.__version__ > "4.0.0":
                    rvecs, tvecs, _ = cv2.aruco.estimatePoseSingleMarkers(
                        [corners[i]], specific_marker_size, self.intrinsic_mat, self.distortion
                    )
                else:
                    rvecs, tvecs = cv2.aruco.estimatePoseSingleMarkers(
                        [corners[i]], specific_marker_size, self.intrinsic_mat, self.distortion
                    )

                pose = Pose()
                pose.position.x = tvecs[0][0][0]
                pose.position.y = tvecs[0][0][1]
                pose.position.z = tvecs[0][0][2]

                rot_matrix = np.eye(4)
                rot_matrix[0:3, 0:3] = cv2.Rodrigues(np.array(rvecs[0][0]))[0]
                quat = tf_transformations.quaternion_from_matrix(rot_matrix)

                pose.orientation.x = quat[0]
                pose.orientation.y = quat[1]
                pose.orientation.z = quat[2]
                pose.orientation.w = quat[3]

                pose_array.poses.append(pose)
                markers.poses.append(pose)
                markers.marker_ids.append(marker_id[0])

            self.poses_pub.publish(pose_array)
            self.markers_pub.publish(markers)

    def start_cb(self, request, response):
        response.success = self.start()
        return response

    def stop_cb(self, request, response):
        response.success = self.stop()
        return response

    def start(self):
        self.get_logger().info("ArucoNode: start called")
        if (self.active):
            return True
        image_topic = (
            self.get_parameter("image_topic").get_parameter_value().string_value
        )
        self.img_sub = self.create_subscription(
            Image, image_topic, self.image_callback, 10
        )
        self.active = True
        return True

    def stop(self):
        self.get_logger().info("ArucoNode: stop called")
        self.active = False
        if (self.img_sub):
            self.destroy_subscription(self.img_sub)
            self.get_logger().info("ArucoNode: stop: img_sub destroyed")
        return True

    def get_marker_size(self, marker_id):
        """Return the marker size for a given marker ID. If a custom size was
        set for the marker_id, return that, else self.marker_size. """
        return self.marker_map.get(marker_id, self.marker_size)

# def main():
#     rclpy.init()
#     node = ArucoNode()
#     rclpy.spin(node)

#     node.destroy_node()
#     rclpy.shutdown()


# if __name__ == "__main__":
#     main()
