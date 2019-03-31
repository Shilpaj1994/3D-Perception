#!/usr/bin/env python

import pickle
import yaml

import numpy as np
import rospy
import sklearn
from sklearn.preprocessing import LabelEncoder
import tf

from geometry_msgs.msg import Pose
from std_msgs.msg import Float64
from std_msgs.msg import Int32
from std_msgs.msg import String
from visualization_msgs.msg import Marker

from pr2_robot.srv import *
from rospy_message_converter import message_converter
from sensor_stick.srv import GetNormals
from sensor_stick.features import compute_color_histograms
from sensor_stick.features import compute_normal_histograms
from sensor_stick.marker_tools import *
from sensor_stick.msg import DetectedObjectsArray
from sensor_stick.msg import DetectedObject
from sensor_stick.pcl_helper import *


def get_normals(cloud):
    """Helper function to get surface normals"""
    get_normals_prox = rospy.ServiceProxy('/feature_extractor/get_normals',
                                          GetNormals)
    return get_normals_prox(cloud).cluster


def make_yaml_dict(test_scene_num, arm_name, object_name, pick_pose, place_pose):
    """Helper function to create a yaml friendly dictionary from ROS messages
    :param test_scene_num: ROS message of the current scene number
    :param arm_name: ROS message holding the arm name
    :param object_name: ROS message holding the object name
    :param pick_pose: ROS message of the pick pose dictionary
    :param place_pose: ROS message of the place pose dictionary
    :return: yaml dictionary
    """

    yaml_dict = {}
    yaml_dict["test_scene_num"] = test_scene_num.data
    yaml_dict["arm_name"]  = arm_name.data
    yaml_dict["object_name"] = object_name.data
    yaml_dict["pick_pose"] = message_converter.convert_ros_message_to_dictionary(pick_pose)
    yaml_dict["place_pose"] = message_converter.convert_ros_message_to_dictionary(place_pose)
    return yaml_dict


def send_to_yaml(yaml_filename, dict_list):
    """Helper function to output the provided dictionary to a yaml file
    :param yaml_filename: String of the name to save the yaml file as
    :param dict_list: Dictionary to save into the yaml file
    """
    data_dict = {"object_list": dict_list}
    with open(yaml_filename, 'w') as outfile:
        yaml.dump(data_dict, outfile, default_flow_style=False)


def pcl_callback(pcl_msg):
    """Callback function for the Point Cloud Subscriber
    :param pcl_msg: ROS point cloud message
    """
# Exercise-2 TODOs:
    # Convert ROS msg to PCL data (XYZRGB)
    cloud = ros_to_pcl(pcl_msg)

    # Statsitical Outlier Filter
    outlier_filter = cloud.make_statistical_outlier_filter()
    # Set the number of neighboring points to analyze for any given point
    outlier_filter.set_mean_k(20)
    # Any point with a mean distance larger than global will be considered out
    outlier_filter.set_std_dev_mul_thresh(0.1)
    cloud_filtered = outlier_filter.filter()

    # Voxel Grid Downsampling
    vox = cloud_filtered.make_voxel_grid_filter()
    LEAF_SIZE = 0.01
    vox.set_leaf_size(LEAF_SIZE, LEAF_SIZE, LEAF_SIZE)
    cloud_filtered = vox.filter()

    # PassThrough Filter to remove the areas on the side of the table
    passthrough_y = cloud_filtered.make_passthrough_filter()
    passthrough_y.set_filter_field_name('y')
    y_axis_min = -0.4
    y_axis_max = 0.4
    passthrough_y.set_filter_limits(y_axis_min, y_axis_max)
    cloud_filtered = passthrough_y.filter()

    # PassThrough Filter to isolate only the objects on the table surface
    passthrough_z = cloud_filtered.make_passthrough_filter()
    passthrough_z.set_filter_field_name('z')
    z_axis_min = 0.61
    z_axis_max = 0.9
    passthrough_z.set_filter_limits(z_axis_min, z_axis_max)
    cloud_filtered = passthrough_z.filter()

    # TODO: RANSAC Plane Segmentation
    seg = cloud_filtered.make_segmenter()
    seg.set_model_type(pcl.SACMODEL_PLANE)
    seg.set_method_type(pcl.SAC_RANSAC)
    max_distance = 0.01
    seg.set_distance_threshold(max_distance)
    inliers, coefficients = seg.segment()

   # TODO: Extract inliers and outliers
    cloud_table = cloud_filtered.extract(inliers, negative=False)
    cloud_objects = cloud_filtered.extract(inliers, negative=True)


    # TODO: Euclidean Clustering
    white_cloud = XYZRGB_to_XYZ(cloud_objects)
    tree = white_cloud.make_kdtree()

    # Create a cluster extraction object
    ec = white_cloud.make_EuclideanClusterExtraction()
    # Set tolerances for distance threshold 
    # as well as minimum and maximum cluster size (in points)
    ec.set_ClusterTolerance(0.05)
    ec.set_MinClusterSize(100)
    ec.set_MaxClusterSize(3000)

    # Search the k-d tree for clusters
    ec.set_SearchMethod(tree)

        # Extract indices for each of the discovered clusters
    cluster_indices = ec.Extract()

    # Create Cluster-Mask Point Cloud to visualize each cluster separately
    # Assign a color corresponding to each segmented object in scene
    cluster_color = get_color_list(len(cluster_indices))

    color_cluster_point_list = []
    detected_objects_labels = []
    detected_objects = []

    # Iterate through each detected object cluster for object recognition
    for index, pts_list in enumerate(cluster_indices):    
        # Store the object's cloud in this list
        object_cluster = []

        # Create an individual cluster just for the object being processed
        for i, pts in enumerate(pts_list):
            # Retrieve cloud values for the x, y, z, rgb object
            object_cluster.append([cloud_objects[pts][0],
                                   cloud_objects[pts][1],
                                   cloud_objects[pts][2],
                                   cloud_objects[pts][3]])
            
            # Retrieve cloud values for the x, y, z object, assigning a
            #   preidentified color to all cloud values
            color_cluster_point_list.append([white_cloud[pts][0],
                                             white_cloud[pts][1],
                                             white_cloud[pts][2],
                                             rgb_to_float(cluster_color[index])])


        # Convert list of point cloud features (x,y,z,rgb) into a point cloud
        pcl_cluster = pcl.PointCloud_PointXYZRGB()
        pcl_cluster.from_list(object_cluster)

        # Convert the cluster from pcl to ROS using helper function
        ros_cloud = pcl_to_ros(pcl_cluster)

        # Extract histogram features (similar to capture_features.py)
        histogram_bins = 128
        chists = compute_color_histograms(ros_cloud,
                                          nbins=histogram_bins,
                                          using_hsv=True)
        normals = get_normals(ros_cloud)
        nhists = compute_normal_histograms(normals,
                                           nbins=histogram_bins)
        feature = np.concatenate((chists, nhists))

        # Make the prediction, retrieve the label for the result and add it
        #   to detected_objects_labels list
        prediction = clf.predict(scaler.transform(feature.reshape(1, -1)))
        label = encoder.inverse_transform(prediction)[0]
        detected_objects_labels.append(label)

        # Publish a label into RViz
        label_pos = list(white_cloud[pts_list[0]])
        label_pos[2] += .4
        object_markers_pub.publish(make_label(label, label_pos, index))

        # Add the detected object to the list of detected objects.
        do = DetectedObject()
        do.label = label
        do.cloud = ros_cloud
        detected_objects.append(do)

    rospy.loginfo('Detected {} objects: {}'.format(len(detected_objects_labels), detected_objects_labels))
    
    # Create new cloud containing all clusters, each with a unique color
    cluster_cloud = pcl.PointCloud_PointXYZRGB()
    cluster_cloud.from_list(color_cluster_point_list)

    # Convert PCL data to ROS messages
    ros_cloud_object_cluster = pcl_to_ros(cluster_cloud)
    ros_cloud_objects = pcl_to_ros(cloud_objects)
    ros_cloud_table = pcl_to_ros(cloud_table)

    # Publish ROS messages of the point clouds and detected objects
    pcl_clustered.publish(ros_cloud_object_cluster) # solid color objects 
    pcl_objects_pub.publish(ros_cloud_objects)      # original color objects
    pcl_table_pub.publish(ros_cloud_table)          # table cloud
    detected_objects_pub.publish(detected_objects)  # detected object labels

    try:
        pr2_mover(detected_objects)
    except rospy.ROSInterruptException:
        pass


def pr2_mover(object_list):
    """Cycle through each object provided in the object list, moving the
    correct arm to pickup the object.
    
    Function to load parameters and request PickPlace service.
    :param object_list: List of detected objects
    """

    # Initialize output list that'll store multiple object yaml dictionaries
    output_list = []

    # Load the parameters from the YAML files located in /pr2_robot/config/
    object_list_param = rospy.get_param('/object_list')
    dropbox_list_param = rospy.get_param('/dropbox')

    # Iterate through all objects that should be moved
    for object_params in object_list_param:
        object_name = object_params['name']
        object_group = object_params['group']

        # Check if the object to be moved was found in the perception analysis,
        #   populating the pick_pose message if it was
        for object_i, object_val in enumerate(object_list):
            if object_name != object_val.label:
                # Skip this object b/c it doesn't match the object to be moved
                continue

            # Assign the scene number
            ros_scene_num = Int32()
            # TODO: Figure out what parameter holds the scene data
            test_num = 3    # World number
            ros_scene_num.data = test_num

            # Assign the object name
            ros_object_name = String()
            ros_object_name.data = object_name

            # Assign the arm that'll be used to pickup the object
            ros_arm_to_use = String()
            if object_group == 'green':
                # The green bin is on the robot's right
                ros_arm_to_use.data = 'right'
            else:
                # The red bin is on the robot's left
                ros_arm_to_use.data = 'left'
            
            # Get the PointCloud for the object and obtain it's centroid
            #   (the average position of all points in the object cloud).
            #   Convert the cloud to an array, then calculate the average
            #   of the array.
            points_array = ros_to_pcl(object_val.cloud).to_array()
            centroid_numpy = np.mean(points_array, axis=0)[:3]
            # Convert the numpy float64 to native python floats
            centroid = [np.asscalar(x) for x in centroid_numpy]

            # Assign the object pick pose
            ros_pick_pose = Pose()
            ros_pick_pose.position.x = centroid[0]
            ros_pick_pose.position.y = centroid[1]
            ros_pick_pose.position.z = centroid[2]

            # Find the correct dropbox's position
            box_pos = [0, 0, 0]     # Set a default box position
            for box_params in dropbox_list_param:
                if box_params['group'] == object_group:
                    box_pos = box_params['position']
                    break
            # TODO: Add a random offset to the dropbox's position

            # Assign the dropbox pose
            ros_place_pos = Pose()
            ros_place_pos.position.x = box_pos[0]
            ros_place_pos.position.y = box_pos[1]
            ros_place_pos.position.z = box_pos[2]

            # Add the object's yaml dict to the output_list
            obj_yaml_dict = make_yaml_dict(ros_scene_num, ros_arm_to_use,
                                           ros_object_name, ros_pick_pose,
                                           ros_place_pos)
            output_list.append(obj_yaml_dict)
            print('processed %s' % ros_object_name.data)


            # Remove the object from object_list to indicate it was picked up
            del object_list[object_i]
            
            # Stop looking through the other identified objects
            break

    # Output your request parameters into an output yaml file
    send_to_yaml('output_%i.yaml' % test_num, output_list)

    

if __name__ == '__main__':
    
    # ROS node initialization
    rospy.init_node('Obj_Detection_Project', anonymous=True)

    # TODO: Create Subscribers
    pcl_sub = rospy.Subscriber("/pr2/world/points", pc2.PointCloud2, pcl_callback, queue_size=1)

    # TODO: Create Publishers
    object_markers_pub = rospy.Publisher("/object_markers", Marker, queue_size=1)
    detected_objects_pub = rospy.Publisher("/detected_objects", DetectedObjectsArray, queue_size=1)

    pcl_objects_pub = rospy.Publisher("/pcl_objects", PointCloud2, queue_size=1)
    pcl_table_pub = rospy.Publisher("/pcl_table", PointCloud2, queue_size=1)
    pcl_clustered = rospy.Publisher("/pcl_cluster", PointCloud2, queue_size=1)


    # TODO: Load Model From disk
    model = pickle.load(open('model_world1.sav', 'rb'))
    clf = model['classifier']
    encoder = LabelEncoder()
    encoder.classes_ = model['classes']
    scaler = model['scaler']

    # Initialize color_list
    get_color_list.color_list = []

    # TODO: Spin while node is not shutdown
    while not rospy.is_shutdown():
        rospy.spin()

