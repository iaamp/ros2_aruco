#!/usr/bin/env python
# Copyright (c) 2023 - present, Alexander Moortgat-Pick & Anna Adamczyk
# All rights reserved.
# contact: moortgat.pick@gmail.com

import os
import sys
import time

import rclpy

from ros2_aruco.aruco_node import ArucoNode

def main(args=None):
    rclpy.init(args=args)

    executor = rclpy.executors.MultiThreadedExecutor(num_threads=2)

    aruco = ArucoNode()

    executor.add_node(aruco)

    executor.spin()

    rclpy.shutdown()

if __name__ == '__main__':
    main()
