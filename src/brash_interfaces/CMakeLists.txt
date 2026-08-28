cmake_minimum_required(VERSION 3.16)
project(brash_interfaces)

if(CMAKE_COMPILER_IS_GNUCXX OR CMAKE_CXX_COMPILER_ID MATCHES "Clang")
  add_compile_options(-Wall -Wextra -Wpedantic)
endif()

find_package(ament_cmake REQUIRED)
find_package(builtin_interfaces REQUIRED)
find_package(rosidl_default_generators REQUIRED)

rosidl_generate_interfaces(${PROJECT_NAME}
  "msg/Capability.msg"
  "msg/RobotState.msg"
  "msg/ExecutionEvent.msg"
  "msg/WorldStateDelta.msg"
  "srv/RegisterRobot.srv"
  "srv/QueryCapabilities.srv"
  "srv/LoadScenario.srv"
  "action/ExecuteTask.action"
  "action/ExecutePrimitive.action"
  DEPENDENCIES builtin_interfaces
)

if(BUILD_TESTING)
  find_package(ament_lint_auto REQUIRED)
  ament_lint_auto_find_test_dependencies()
endif()

ament_package()
