#!/usr/bin/env python3

# 3rd partys
import numpy as np
import pandas as pd
import os
import csv
from ament_index_python.packages import get_package_share_directory
from cv_bridge import CvBridge
import cv2
from ultralytics import YOLO
from geometry_msgs.msg import Twist
import time
import math


# ros
import rclpy
from rclpy.node import Node
from sensor_msgs.msg import LaserScan, Image

from rclpy.action import ActionServer, CancelResponse
from rclpy.action import ActionClient
from rcl_interfaces.msg import ParameterType
from action_msgs.msg import GoalStatus
from nav_msgs.msg import Odometry
from rclpy.qos import QoSProfile, ReliabilityPolicy


# messages
from std_msgs.msg import Float32MultiArray
from nav_msgs.msg import OccupancyGrid
from nav_msgs.msg import MapMetaData
from nav2_msgs.action import NavigateToPose

from matplotlib import pyplot as plt


class LaserSubscriber(Node):
    def __init__(self):
        super().__init__('laser_subscriber')
        self.subscription = self.create_subscription(
            LaserScan, 'scan', self.listener_callback, 10)
        self.ranges = [0.0] * 640
        self.forward_distance = 1000.0
        self.left_forward_distance = 1000.0
        self.right_forward_distance = 1000.0
        self.left_distance = 1000.0
        self.right_distance = 1000.0
        self.back_distance = 1000.0
        self.accumulated_distance = 0.0
        # closest obstacle distance forward (0-180 degrees)
        self.closest_obstacle_distance = 1000.0
        self.subscription  # prevent unused variable warning
        self.size = 0

    def listener_callback(self, msg):
        self.ranges = msg.ranges
        self.size = len(msg.ranges)
        self.left_distance = msg.ranges[self.size//2]
        self.left_forward_distance = msg.ranges[3*self.size//8]
        self.forward_distance = msg.ranges[self.size//4]
        self.right_forward_distance = msg.ranges[self.size//8]
        self.right_distance = msg.ranges[0]
        self.back_distance = msg.ranges[3*self.size//4]
        self.closest_obstacle_distance = min(msg.ranges[0:self.size//2])
        print(f"Left: {self.left_distance:.2f}, Forward: {self.forward_distance:.2f}, Right: {self.right_distance:.2f}, Back: {self.back_distance:.2f}")
        print(f"Closest obstacle distance: {self.closest_obstacle_distance:.2f}")


class CmdVelPublisher(Node):
    def __init__(self):
        super().__init__('cmd_vel_publisher')
        self.publisher_ = self.create_publisher(Twist, 'cmd_vel', 10)


class OdomSubscriber(Node):
    def __init__(self):
        super().__init__('odom_subscriber')
        qos_profile = QoSProfile(depth=10)
        qos_profile.reliability = ReliabilityPolicy.BEST_EFFORT
        self.subscription = self.create_subscription(
            Odometry, 'odom', self.listener_callback, qos_profile)
        self.odom_buffer = []
        self.current_position = None
        self.subscription  # prevent unused variable warning

    def listener_callback(self, msg):
        self.odom_buffer.append(msg)
        if len(self.odom_buffer) > 10:
            self.odom_buffer.pop(0)  # Keep only the last 10 odometry messages
        self.current_position = msg.pose.pose.position


class NavigationClient(Node):
    def __init__(self):
        super().__init__('navigation_client')
        self._action_client = ActionClient(
            self, NavigateToPose, 'navigate_to_pose')
        # a cartographer subscription is created to access the occupancy
        self.cartographer = CartographerSubscriber()
        rclpy.spin_once(self.cartographer)
        # a subscription to the odometry is created to access the robot position
        self.subscription = OdomSubscriber()
        rclpy.spin_once(self.subscription)
        self.last_photo_pose = None  # this variable is used to store the last photo pose
        # grid and determine which positions to navigate to

    def goal_response_callback(self, future):
        goal_handle = future.result()
        if not goal_handle.accepted:
            self.get_logger().info('Exploration goal rejected')
            return

        self.get_logger().info('Navigation goal accepted')

        self._get_result_future = goal_handle.get_result_async()
        self._get_result_future.add_done_callback(self.get_result_callback)

    def get_result_callback(self, future):
        result = future.result().result
        status = future.result().status
        if status == GoalStatus.STATUS_SUCCEEDED:
            self.get_logger().info('Arrived at destination')
        else:
            self.get_logger().info(
                'Goal failed with status: {0}'.format(status))

        rclpy.spin_once(self.cartographer)
        rclpy.spin_once(self.subscription)

    def distance(self, p1, p2):
        return ((p1.x - p2.x)**2 + (p1.y - p2.y)**2)**0.5

    def send_goal(self, ball_position_subscriber, cmd_vel_publisher, command=Twist()):
        self.get_logger().info('Waiting for action server...')
        self._action_client.wait_for_server()

        # grab the first waypoint and remove it from the list of waypoints
        waypoint = self.cartographer.sorted_accessible_waypoints[0]
        self.cartographer.sorted_accessible_waypoints = self.cartographer.sorted_accessible_waypoints[
            1:]
        
        goal_msg = NavigateToPose.Goal()
        goal_msg.pose.header.frame_id = 'odom'
        goal_msg.pose.pose.position.x = float(
            waypoint[0] + self.cartographer.origin[0])
        goal_msg.pose.pose.position.y = float(
            waypoint[1] + self.cartographer.origin[1])
        self.last_photo_pose = self.get_current_position()  # save the last photo pose
        # goal_msg.pose.pose.orientation.w = 1.0

        self.get_logger().info(
            'Sending navigation goal request x: ' + str(round(goal_msg.pose.pose.position.x, 2)) + ' y: ' + str(
                round(goal_msg.pose.pose.position.y, 2)))

        self._send_goal_future = self._action_client.send_goal_async(goal_msg)
        self._send_goal_future.add_done_callback(self.goal_response_callback)

        rclpy.spin_until_future_complete(self, self._send_goal_future)

        goal_handle = self._send_goal_future.result()
        get_result_future = goal_handle.get_result_async()

        # rclpy.spin_until_future_complete(self, get_result_future)
        position = self.get_current_position()
        if position is not None:
            self.get_logger().info(
                f'Robot position -> x: {position.x}, y: {position.y}, z: {position.z}')

        while not get_result_future.done():
            rclpy.spin_once(self)
            rclpy.spin_once(ball_position_subscriber)
            if ball_position_subscriber.ball_position is not None:
                print("Ball position detected, stopping navigation.")
                break
            current_pos = self.get_current_position()
            if current_pos is None:
                self.get_logger().info("No odometry data available yet.")
                continue
            self.get_logger().info(
                f'Current goal position -> x: {goal_msg.pose.pose.position.x}, y: {goal_msg.pose.pose.position.y}')
            self.get_logger().info(
                f'Distance to goal: {self.distance(current_pos, goal_msg.pose.pose.position):.2f} meters')
            self.get_logger().info(
                f'Distance to last photo pose: {self.distance(current_pos, self.last_photo_pose):.2f} meters')

            if current_pos and self.distance(current_pos, self.last_photo_pose) >= 3.0:
                self.get_logger().info("3 meters passed — taking photo sequence.")

                # Cancel goal
                self.get_logger().info("Cancelling current goal...")
                cancel_future = goal_handle.cancel_goal_async()
                rclpy.spin_until_future_complete(self, cancel_future)
                cancel_result = cancel_future.result()
                print(cancel_result)
                self.get_logger().info("Goal cancelled successfully.")

                spin_detect_ball(self.subscription, cmd_vel_publisher,
                                 command, ball_position_subscriber)
                # Resend goal
                self._send_goal_future = self._action_client.send_goal_async(
                    goal_msg)
                rclpy.spin_until_future_complete(self, self._send_goal_future)
                goal_handle = self._send_goal_future.result()

                if not goal_handle.accepted:
                    self.get_logger().error("Goal was rejected after photo.")
                    return

                result_future = goal_handle.get_result_async()
                self.last_photo_pose = current_pos  # reset distance tracking

        rclpy.spin_once(self.cartographer)
        print("Goal completed or cancelled.")

    def get_current_position(self):
        """
        This function gets the current position of the robot in the map.
        :return: current position of the robot in the map
        """
        rclpy.spin_once(self.subscription)
        position = self.subscription.current_position
        if position is not None:
            return position
        else:
            self.get_logger().info("No odometry data available yet.")
            return None


class VisualCoverageSubscriber(Node):
    def __init__(self):
        super().__init__('visual_coverage_subscriber')
        self.subscription = self.create_subscription(
            OccupancyGrid, 'visual_coverage_map', self.listener_callback, 10)
        self.coverage_map = None
        self.map_info = None
        self.subscription  # prevent unused variable warning

    def listener_callback(self, msg):
        self.coverage_map = np.array(msg.data).reshape(
            (msg.info.height, msg.info.width))
        self.map_info = msg.info
        self.get_logger().info('Received visual coverage map.')


class CartographerSubscriber(Node):
    def __init__(self):
        super().__init__('cartographer_subscriber')
        self.occupancy_subscription = self.create_subscription(
            OccupancyGrid, 'map', self.occupancy_callback, 10)

        self.waypoints = self.generate_list_of_waypoints(
            n_of_waypoints=100, step=0.2)
        self.accessible_waypoints = np.array([])
        self.sorted_accessible_waypoints = np.array([])
        self.occupancy_value = np.array([])
        self.origin = np.array([0.0, 0.0])
        # a visual coverage subscriber is created to access the visual coverage map
        self.visual_node = VisualCoverageSubscriber()
        rclpy.spin_once(self.visual_node)  # refresh the visual coverage map

    def occupancy_callback(self, msg):
        """

        The cartographer subscriber callback function refreshes the list of accessible waypoints. It sorts them and
        saves them in the self.sorted_accessible_waypoints variable.

        :param msg: OccupancyGrid message. Includes map metadata and an array with the occupancy probability values
        :return: None
        """

        data = np.array(msg.data)  # download the occupancy grid
        current_map_width = msg.info.width  # get the current map width
        self.get_logger().info(f"Current map width: {current_map_width}")
        current_map_height = msg.info.height  # get the current map height
        self.get_logger().info(f"Current map height: {current_map_height}")

        resolution = msg.info.resolution  # get the resolution
        origin = msg.info.origin.position  # get the origin of the map
        # save the origin in a numpy array
        self.origin = np.array([origin.x, origin.y])
        # get the map origin in the occupancy grid
        origin_x = int((origin.x) / resolution)
        origin_y = int((origin.y) / resolution)

        # reshape the data so it resembles the map shape
        data = np.reshape(data, (current_map_height, current_map_width))
        self.waypoints = self.generate_waypoints_from_map(
            current_map_width, current_map_height, resolution)
        waypoints_height = max(self.waypoints[:, 0])
        waypoints_width = max(self.waypoints[:, 1])
        self.get_logger().info(
            f"Waypoints height: {waypoints_height}, Waypoints width: {waypoints_width}")

        # Here we go through every waypoint and save the ones that are accessible.
        # An accessible waypoint is one which has no obstacles, and has few or no unknown squares in the vicinity.
        self.accessible_waypoints = np.array([])
        self.occupancy_value = np.array([])
        unaccessible_waypoints = np.array([])
        for waypoint in self.waypoints:
            try:
                occupancy_grid_coordinates = [int((waypoint[1]) / resolution), int((waypoint[0]) /
                                                                                   resolution)]

                accessible, score = self.convolute(
                    # perform convolution
                    data, self.visual_node.coverage_map, occupancy_grid_coordinates, size=5, occ_threshold=40)

                # if the convolution returns True, it means the WP is accessible, so it is stored in
                # self.accessible_waypoints
                if accessible:
                    self.accessible_waypoints = np.append(
                        self.accessible_waypoints, waypoint)
                    self.occupancy_value = np.append(
                        self.occupancy_value, score)  # store the score of the WP
                else:
                    # if the convolution returns False, it means the WP is not accessible, so it is stored in
                    # self.unaccessible_waypoints
                    unaccessible_waypoints = np.append(
                        unaccessible_waypoints, waypoint)
            # because the waypoint array is over-sized, we need to remove the values that are out of range

            except IndexError:
                pass

            # scatter the accessible and unaccessible waypoints in the map with different colors

        # reshape the accessible waypoints array to shape (n, 2)
        self.accessible_waypoints = self.accessible_waypoints.reshape(-1, 2)

        # Sorting...
        occupancy_value_idxs = self.occupancy_value.argsort()
        self.sorted_accessible_waypoints = self.accessible_waypoints[occupancy_value_idxs[::-1]]

        # Default fallback waypoints
        if np.size(self.sorted_accessible_waypoints) == 0:
            self.sorted_accessible_waypoints = np.array(
                [[1.5, 0.0], [0.0, 1.5], [-1.5, 0.0], [0.0, -1.5]])

        self.get_logger().info('Accessible waypoints have been updated...')

        # --- ⬇️ Visualization Part Starts Here ⬇️ ---
        plt.figure(figsize=(8, 8))
        # Draw the occupancy map
        data_img = np.copy(data)
        data_img[data_img == -1] = 128  # unknown -> gray
        plt.imshow(data_img, cmap='gray', origin='lower')  # Map

        # Draw waypoints
        if self.accessible_waypoints.shape[0] > 0:
            plt.scatter(
                (self.accessible_waypoints[:, 0]) / msg.info.resolution,
                (self.accessible_waypoints[:, 1]) / msg.info.resolution,
                c='green', s=5, label='Accessible'
            )

        if unaccessible_waypoints.shape[0] > 0:
            unaccessible_waypoints = unaccessible_waypoints.reshape(-1, 2)
            plt.scatter(
                (unaccessible_waypoints[:, 0]) / msg.info.resolution,
                (unaccessible_waypoints[:, 1]) / msg.info.resolution,
                c='red', s=5, label='Unaccessible'
            )

        # Draw navigation goal (first in sorted accessible list)
        if self.sorted_accessible_waypoints.shape[0] > 0:
            nav_goal = self.sorted_accessible_waypoints[0]
            plt.scatter(
                [(nav_goal[0]) / msg.info.resolution],
                [(nav_goal[1]) / msg.info.resolution],
                marker='*', c='blue', s=100, label='Navigation Goal'
            )

        # Draw origin
        plt.scatter(
            [self.origin[0] / msg.info.resolution],
            [self.origin[1] / msg.info.resolution],
            marker='o', c='yellow', s=100, label='Origin'
        )

        plt.legend()
        plt.title('Map with Accessible Waypoints and Navigation Goal')
        plt.xlabel('Map X (cells)')
        plt.ylabel('Map Y (cells)')
        plt.grid(False)
        plt.tight_layout()
        plt.pause(0.001)
        plt.clf()

    @staticmethod
    def convolute(data, coverage_map, coordinates, size=3, occ_threshold=40, coverage_weight=0.5):
        """
        Performs a convolution operation on the occupancy grid data to determine if a waypoint is accessible.
        """
        occ_sum = 0
        coverage_sum = 0

        for x in range(int(coordinates[0] - size / 2), int(coordinates[0] + size / 2)):
            for y in range(int(coordinates[1] - size / 2), int(coordinates[1] + size / 2)):
                if data[x, y] == -1:
                    occ_sum += 100  # unknown area
                elif data[x, y] > 50:
                    occ_sum += 1000000  # obstacle
                else:
                    occ_sum += data[x, y]

                # encourage going to unseen places (0 = unseen, 1 = seen)
                coverage_sum += 1 - coverage_map[x, y]  # high if unseen

        area = size * size
        occ_avg = occ_sum / area
        coverage_avg = coverage_sum / area  # 0 if fully seen, 1 if fully unseen

        if occ_avg < occ_threshold:
            score = (1 - coverage_weight) * occ_avg + \
                coverage_weight * (100 * coverage_avg)
            return True, score
        else:
            return False, float('inf')

    def generate_list_of_waypoints(self, n_of_waypoints, step):
        """

        Generates a grid of waypoints of size ('n_of_waypoints' * 'n_of_waypoints') and step size 'step'

        :param n_of_waypoints: number of total waypoints to generate per side
        :param step: float resolution of the waypoints
        :return waypoints: 2D numpy array of a list of coordinates of size dim x 2,
        where dim is the number of waypoints
        """

        waypoints = np.zeros((n_of_waypoints * n_of_waypoints, 2))

        i = 0
        for index_y in range(n_of_waypoints):
            for index_x in range(n_of_waypoints):
                waypoints[i] = [float(index_x) / (1/step),
                                float(index_y) / (1/step)]
                i += 1

        self.get_logger().info("Grid of waypoints has been generated.")

        return waypoints

    def generate_waypoints_from_map(self, map_width, map_height, resolution, step=0.2):
        """
        Generate waypoints that span the full map area based on its current size, origin, and resolution.
        :param map_width: Width of the map in cells
        :param map_height: Height of the map in cells
        :param resolution: Size of one cell in meters
        :param origin: [x, y] origin of the map in world coordinates
        :param step: Distance between waypoints in meters
        :return: np.ndarray of waypoints in (x, y) format
        """
        x_min = 0
        y_min = 0
        x_max = x_min + map_width * resolution
        y_max = y_min + map_height * resolution

        x_coords = np.arange(x_min, x_max, step)
        y_coords = np.arange(y_min, y_max, step)

        waypoints = np.array([[x, y] for x in x_coords for y in y_coords])

        return waypoints


def reset_commands(command: Twist) -> Twist:
    """Resets all Twist commands to zero."""
    command.linear.x = 0.0
    command.linear.y = 0.0
    command.linear.z = 0.0
    command.angular.x = 0.0
    command.angular.y = 0.0
    command.angular.z = 0.0
    return command


def spin_detect_ball(subscriber: LaserSubscriber, publisher: CmdVelPublisher, command: Twist, camera_subscriber):
    """
    Makes the robot spin 360 degrees, stopping every 60 degrees to check for a ball using YOLO.
    """
    command = reset_commands(command)
    publisher.get_logger().info("Starting 360° spin with 6 detection checks...")

    ball_detected = False

    angular_speed = 2.0  # radians per second
    angle_per_step = math.pi / 6  # 60 degrees = π/3 radians
    spin_time_per_step = angle_per_step / \
        abs(angular_speed)  # time to rotate 60 degrees

    for step in range(6):
        publisher.get_logger().info(
            f"Step {step + 1} of 6: Rotating {math.degrees(angle_per_step)} degrees...")

        # Start rotating
        command = reset_commands(command)
        command.angular.z = angular_speed
        publisher.publisher_.publish(command)

        # Wait exactly the needed time
        start_time = time.time()
        print(f"Rotating for {spin_time_per_step:.2f} seconds...")
        while time.time() - start_time < 2*spin_time_per_step:
            # print(f"Rotating... {time.time() - start_time:.2f} seconds elapsed")
            # rclpy.spin_once(subscriber)
            # print("Checking for obstacles...")
            # rclpy.spin_once(camera_subscriber)
            pass

        # Stop rotation
        command = reset_commands(command)
        publisher.publisher_.publish(command)
        print("Waiting for 1 seconds to stabilize...")
        time.sleep(1)  # Wait for 1 second to stabilize

        if camera_subscriber.ball_position is not None:
            publisher.get_logger().info(
                f"Ball position detected: {camera_subscriber.ball_position}")
            ball_detected = True
            break

        publisher.get_logger().info(f"Checking for ball at step {step + 1}...")

    command = reset_commands(command)
    publisher.publisher_.publish(command)
    publisher.get_logger().info("Finished 360° spin.")

    return ball_detected


class BallPositionSubscriber(Node):
    def __init__(self):
        super().__init__('ball_position_subscriber')
        self.subscription = self.create_subscription(
            Float32MultiArray,
            'ball_position',
            self.listener_callback,
            10
        )
        self.ball_position = None
        self.subscription  # prevent unused variable warning

    def listener_callback(self, msg):
        if msg.data[0] != 0.0 and msg.data[1] != 0.0:
            self.ball_position = (msg.data[0], msg.data[1])
            self.get_logger().info(
                f"Initial ball position set: {self.ball_position}")
        else:
            self.get_logger().info(
                f"Received new ball position: {msg.data[0]}, {msg.data[1]}")
        self.get_logger().info(f"Ball position received: {self.ball_position}")


def main(args=None):
    rclpy.init(args=args)

    print("Starting the navigation client...")
    navigation = NavigationClient()
    laser_subscriber = LaserSubscriber()
    # camera_subscriber = CameraSubscriber()
    cmd_vel_publisher = CmdVelPublisher()
    ball_position_subscriber = BallPositionSubscriber()  # Nouvelle instance
    command = Twist()

    print("Navigation client started.")
    print("Will spin the ball position subscriber...")
    rclpy.spin_once(ball_position_subscriber)
    print("Ball position subscriber spun once.")
    print("Starting the navigation loop...")

    while rclpy.ok():
        navigation.send_goal(ball_position_subscriber,
                             cmd_vel_publisher, command)
        rclpy.spin_once(ball_position_subscriber)
        if ball_position_subscriber.ball_position is None and spin_detect_ball(laser_subscriber, cmd_vel_publisher, command, ball_position_subscriber):
            navigation.get_logger().info("Ball detected, stopping navigation.")
        if ball_position_subscriber.ball_position is not None:
            navigation.get_logger().info(
                f"Ball position: {ball_position_subscriber.ball_position}")
            # navigate to the ball position
            goal_msg = NavigateToPose.Goal()
            goal_msg.pose.header.frame_id = 'odom'
            goal_msg.pose.pose.position.x = ball_position_subscriber.ball_position[0]
            goal_msg.pose.pose.position.y = ball_position_subscriber.ball_position[1]
            # Assuming no specific orientation is required
            goal_msg.pose.pose.orientation.w = 1.0

            navigation.get_logger().info(
                f"Sending navigation goal to ball position x: {goal_msg.pose.pose.position.x}, y: {goal_msg.pose.pose.position.y}"
            )

            navigation._send_goal_future = navigation._action_client.send_goal_async(
                goal_msg)
            rclpy.spin_until_future_complete(
                navigation, navigation._send_goal_future)

            goal_handle = navigation._send_goal_future.result()
            if not goal_handle.accepted:
                navigation.get_logger().error("Goal to ball position was rejected.")
                continue

            navigation.get_logger().info("Goal to ball position accepted.")
            result_future = goal_handle.get_result_async()
            rclpy.spin_until_future_complete(navigation, result_future)

            if result_future.result().status == GoalStatus.STATUS_SUCCEEDED:
                navigation.get_logger().info("Arrived at ball position.")
                break
            else:
                navigation.get_logger().info("Failed to reach ball position.")
        else:
            navigation.get_logger().info("No ball position received yet, continuing navigation...")

          # Vérifie les mises à jour de la position de la balle
    print("Navigation loop finished.")

    navigation.destroy_node()
    laser_subscriber.destroy_node()
    ball_position_subscriber.destroy_node()  # Détruire le nouveau subscriber
    rclpy.shutdown()


if __name__ == '__main__':
    main()
