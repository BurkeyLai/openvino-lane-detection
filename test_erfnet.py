from __future__ import print_function, division

import logging
import os
import sys
from argparse import ArgumentParser, SUPPRESS
from math import exp as exp
from time import time

import cv2
import PIL.Image as pil
import numpy as np
from openvino.inference_engine import IENetwork, IECore

selected_lines = 2

logging.basicConfig(format="[ %(levelname)s ] %(message)s", level=logging.INFO, stream=sys.stdout)
log = logging.getLogger()


def build_argparser():
    parser = ArgumentParser(add_help=False)
    args = parser.add_argument_group('Options')
    args.add_argument('-h', '--help', action='help', default=SUPPRESS, help='Show this help message and exit.')
    args.add_argument("-m", "--model", help="Required. Path to an .xml file with a trained model.",
                      required=True, type=str)
    args.add_argument("-i", "--input", help="Required. Path to an image/video file. (Specify 'cam' to work with "
                                            "camera)", required=True, type=str)
    args.add_argument("-l", "--cpu_extension",
                      help="Optional. Required for CPU custom layers. Absolute path to a shared library with "
                           "the kernels implementations.", type=str, default=None)
    args.add_argument("-d", "--device",
                      help="Optional. Specify the target device to infer on; CPU, GPU, FPGA, HDDL or MYRIAD is"
                           " acceptable. The sample will look for a suitable plugin for device specified. "
                           "Default value is CPU", default="CPU", type=str)
    args.add_argument("--labels", help="Optional. Labels mapping file", default=None, type=str)
    args.add_argument("-t", "--prob_threshold", help="Optional. Probability threshold for detections filtering",
                      default=0.5, type=float)
    args.add_argument("-iout", "--iou_threshold", help="Optional. Intersection over union threshold for overlapping "
                                                       "detections filtering", default=0.4, type=float)
    args.add_argument("-ni", "--number_iter", help="Optional. Number of inference iterations", default=1, type=int)
    args.add_argument("-pc", "--perf_counts", help="Optional. Report performance counters", default=False,
                      action="store_true")
    args.add_argument("-r", "--raw_output_message", help="Optional. Output inference results raw values showing",
                      default=False, action="store_true")
    return parser

def softmax(X, theta = 1.0, axis = None):
    """
    Compute the softmax of each element along an axis of X.

    Parameters
    ----------
    X: ND-Array. Probably should be floats.
    theta (optional): float parameter, used as a multiplier
        prior to exponentiation. Default = 1.0
    axis (optional): axis to compute values along. Default is the
        first non-singleton axis.

    Returns an array the same size as X. The result will sum to 1
    along the specified axis.
    ----------
    https://nolanbconaway.github.io/blog/2017/softmax-numpy.html
    """

    # make X at least 2d
    y = np.atleast_2d(X)

    # find axis
    if axis is None:
        axis = next(j[0] for j in enumerate(y.shape) if j[1] > 1)

    # multiply y against the theta parameter,
    y = y * float(theta)

    # subtract the max for numerical stability
    y = y - np.expand_dims(np.max(y, axis = axis), axis)

    # exponentiate y
    y = np.exp(y)

    # take the sum along the specified axis
    ax_sum = np.expand_dims(np.sum(y, axis = axis), axis)

    # finally: divide elementwise
    p = y / ax_sum

    # flatten if X was 1D
    if len(X.shape) == 1: p = p.flatten()

    return p

def _thresh_coord(coord):
    pts_x = coord[:, 0]
    mean_x = np.mean(pts_x)
    idx = np.where(np.abs(pts_x - mean_x) < mean_x)
    return coord[idx[0]]

def get_lane_mask(instance_seg):
    
    mask_list = []
    
    for i in range(selected_lines):
        f = instance_seg[:, :, i]
        mask_img = []
        mask_img = cv2.cvtColor(np.asarray(pil.new(mode = 'RGBA', size = (f.shape[1], f.shape[0]))), cv2.COLOR_RGBA2RGB)
        mask_img = cv2.cvtColor(mask_img, cv2.COLOR_RGB2GRAY)
        mask_img = mask_img.astype(np.uint8)
        
        frame = (f * 255).astype(np.uint8)
        idx = np.where(frame >= 60)
        if len(idx[0]) > 0 and len(idx[1]) > 0:
            row = idx[0]
            column = idx[1]
            max_X = max(column) if len(column > 0) else f.shape[1]
            min_X = min(column) if len(column > 0) else 0
            max_Y = max(row) if len(row > 0) else f.shape[0]
            min_Y = min(row) if len(row > 0) else f.shape[0] / 2

            coord = np.concatenate(([idx[0]], [idx[1]]), axis = 0).transpose((1, 0))
            coord = np.flip(coord, axis=1)
            coord = _thresh_coord(coord)
            
            ############# Recieve lane line
            X = []
            Y = np.unique(coord[:, 1])
            for y in Y:
                idx_x = np.where(coord[:, 1] == y)
                X.append(np.mean(coord[idx_x, 0]))
            X = np.array(X)
            line = np.concatenate(([X], [Y]), axis = 0).transpose((1, 0))

            ############# Recieve 1st order polynomial line
            if len(X) > 5 and len(Y) > 5:

                C = np.polyfit(Y, X, 1)
                poly_Y = np.arange(min_Y, max_Y, 1)
                poly_X = (C[0] * poly_Y + C[1])
            
                poly_line = np.concatenate(([poly_X], [poly_Y]), axis = 0).transpose((1, 0))
                poly_line = poly_line.astype(int)

                ############# Delete those points which are far from polynomial line
                D = np.abs(C[0] * line[:, 1] - line[:, 0] + C[1]) / np.sqrt(C[0] ** 2 + 1)
                D_idx = np.where(D > 5)
                line = np.delete(line, D_idx, axis = 0)
            
            line = line.astype(int)
            cv2.polylines(img = mask_img, pts = [line], isClosed = False, color = (255, 255, 255), thickness = 5)

            mask_list.append(mask_img)
        else:
            mask_list.append(mask_img)
            continue

    return mask_list

def main():
    args = build_argparser().parse_args()

    model_xml = args.model
    model_bin = os.path.splitext(model_xml)[0] + ".bin"
    mean = ([103.939, 116.779, 123.68], (0, ))
    std = ([1, 1, 1], (1, ))

    # ------------- 1. Plugin initialization for specified device and load extensions library if specified -------------
    log.info("Creating Inference Engine...")
    ie = IECore()
    if args.cpu_extension and 'CPU' in args.device:
        ie.add_extension(args.cpu_extension, "CPU")

    # -------------------- 2. Reading the IR generated by the Model Optimizer (.xml and .bin files) --------------------
    log.info("Loading network files:\n\t{}\n\t{}".format(model_xml, model_bin))
    net = IENetwork(model = model_xml, weights = model_bin)

    # ---------------------------------- 3. Load CPU extension for support specific layer ------------------------------
    if "CPU" in args.device:
        supported_layers = ie.query_network(net, "CPU")
        not_supported_layers = [l for l in net.layers.keys() if l not in supported_layers]
        if len(not_supported_layers) != 0:
            log.error("Following layers are not supported by the plugin for specified device {}:\n {}".
                      format(args.device, ', '.join(not_supported_layers)))
            log.error("Please try to specify cpu extensions library path in sample's command line parameters using -l "
                      "or --cpu_extension command line argument")
            sys.exit(1)

    assert len(net.inputs.keys()) == 1, "Sample supports only YOLO V3 based single input topologies"

    # ---------------------------------------------- 4. Preparing inputs -----------------------------------------------
    log.info("Preparing inputs")
    input_blob = next(iter(net.inputs))

    #  Defaulf batch_size is 1
    net.batch_size = 1

    # Read and pre-process input images
    n, c, h, w = net.inputs[input_blob].shape
    
    if args.labels:
        with open(args.labels, 'r') as f:
            labels_map = [x.strip() for x in f]
    else:
        labels_map = None

    input_stream = 0 if args.input == "cam" else args.input

    cap = cv2.VideoCapture(input_stream)

    # ----------------------------------------- 5. Loading model to the plugin -----------------------------------------
    log.info("Loading model to the plugin")
    exec_net = ie.load_network(network=net, num_requests=2, device_name=args.device)

    cur_request_id = 0
    next_request_id = 1
    render_time = 0
    parsing_time = 0

    # ----------------------------------------------- 6. Doing inference -----------------------------------------------
    
    if(cap.isOpened() == False): 
        print('Error opening video stream or file')
    else:
        log.info("Starting inference...")
        print("To close the application, press 'CTRL+C' here or switch to the output window and press ESC key")
        while cap.isOpened():
            ret, frame = cap.read()

            if not ret:
                break

            frame_vis = frame
            h = frame_vis.shape[0]
            w = frame_vis.shape[1]
            frame = frame[240:, :, :]
            o_h = frame.shape[0]
            o_w = frame.shape[1]
            frame = cv2.resize(frame, (976, 208), interpolation = cv2.INTER_LINEAR)
            # ----------------------------------------------- Group Normalize -----------------------------------------------
            
            img_group = [frame]
            out_images = list()
            for img, m, s in zip(img_group, mean, std):
                if len(m) == 1: # single channel image
                    img = img - np.array(m)  
                    img = img / np.array(s)
                else:
                    img = img - np.array(m)[np.newaxis, np.newaxis, ...]
                    img = img / np.array(s)[np.newaxis, np.newaxis, ...]
                out_images.append(img)

            pred_input = []
            frame = out_images[0]
            frame_show = frame
            frame = frame.transpose((2, 0, 1)).astype(np.float32).reshape(1, 3, 208, 976)
            # ----------------------------------------------- Run the net -----------------------------------------------
            inf_start = time()
            outputs = exec_net.infer({'input.1': frame})
            det_time = time() - inf_start
            print('FPS: {}'.format(1 / det_time))
            # ----------------------------------------------- Fetch detected center lane -----------------------------------------------
            outputs = outputs['ConvTranspose_208'].reshape(5, 208, 976)
            outputs = softmax(outputs, axis = 0)
            pred_input.append(outputs[2])
            pred_input.append(outputs[3])
            pred_input = np.array(pred_input)
            pred_input = cv2.resize(pred_input.transpose((1, 2, 0)), (448, 208), interpolation = cv2.INTER_LINEAR) # (208, 976, 3) -> (208, 448, 3)
            mask_list = get_lane_mask(pred_input[:, :, :])
            # ----------------------------------------------- Post processing -----------------------------------------------
            for m in range(selected_lines):
                tmp = mask_list[m]
                mask_list[m] = []
                mask_list[m] = cv2.resize(tmp, (o_w, o_h), interpolation = cv2.INTER_LINEAR) # (208, 448) -> (o_h, o_w)
            
            mask_output = np.zeros([o_h, o_w, 3]).astype(np.uint8)
            mask_output[:, :, 0] = np.zeros([o_h, o_w]).astype(np.uint8) # B
            mask_output[:, :, 1] = mask_list[0].astype(np.uint8) # G
            mask_output[:, :, 2] = mask_list[1].astype(np.uint8) # R
            
            mask_frame = np.zeros([h, w, 3]).astype(np.uint8)
            for i in range(0, 3):
                compensate_zero = np.zeros([240, o_w]).astype(np.uint8)
                mask_frame[:, :, i] = np.concatenate((compensate_zero, mask_output[:, :, i]), axis = 0)
            
            output_frame = (cv2.addWeighted(frame_vis, 1, mask_frame, 1, 0))
            cv2.imshow("DetectionResults", output_frame)

            key = cv2.waitKey(1)

            # ESC key
            if key == 27:
                break

        cap.release()
        cv2.destroyAllWindows()

if __name__ == '__main__':
    sys.exit(main() or 0)
