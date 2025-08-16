# basic-task

# TurtleBot4 Navigation Project

Website: [https://basic-task-tan.vercel.app/](https://basic-task-tan.vercel.app/)

---

## How to Run the Project

All relevant code files are located in the `/code` directory.

To launch and run the system, execute the following commands in separate terminals or appropriate environments:

1. **Launch SLAM (Simultaneous Localization and Mapping):**

   ```bash
   ros2 launch turtlebot4_navigation slam.launch.py
   ```

   This command starts the SLAM system, enabling the robot to build a map of its environment while tracking its position.

2. **Visualize the Robot:**

   ```bash
   ros2 launch turtlebot4_viz view_robot.launch.py
   ```

   This launches the visualization tool to view the robot and its sensors in real-time (usually via RViz).

3. **Start Navigation Stack:**

   ```bash
   ros2 launch nav2_bringup navigation_launch.py
   ```

   This command activates the navigation system for path planning and robot movement control.

4. **Run Visual Coverage Mapper:**

   ```bash
   python3 -m visual_coverage_mapper
   ```

   This Python module tracks which areas have been visually covered by the robot’s camera to improve exploration and object detection.

5. **Run Discoverer Module:**

   ```bash
   python3 -m discoverer
   ```

   This module manages the robot’s exploration logic, coordinating movement and data collection.

6. **Run Depth Processing (on Robot Raspberry Pi):**

   ```bash
   python3 -m depth
   ```

   This module processes depth sensor data on the Raspberry Pi onboard the robot, providing 3D perception.

---

Make sure your environment is set up with ROS 2 and all dependencies installed before running the commands.
