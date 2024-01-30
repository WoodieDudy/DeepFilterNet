import copy
import onnx
import argparse
import subprocess

import torch
import torchaudio
import numpy as np
import onnxruntime as ort
import torch.utils.benchmark as benchmark

from torch_df_streaming_minimal import TorchDFMinimalPipeline
from typing import Dict, Iterable
from torch.profiler import profile, ProfilerActivity
from onnxruntime.quantization import quantize_dynamic, QuantType, shape_inference

# Error with compatability - https://github.com/microsoft/onnxruntime/issues/19323
onnx.helper.make_sequence_value_info = onnx.helper.make_tensor_sequence_value_info

torch.manual_seed(0)

FRAME_SIZE = 480
INPUT_NAMES = [
    "input_frame",
]
OUTPUT_NAMES = ["enhanced_audio_frame", "out_states"]


def onnx_simplify(
    path: str, input_data: Dict[str, np.ndarray], input_shapes: Dict[str, Iterable[int]]
) -> str:
    """
    Simplify ONNX model using onnxsim and checking it

    Parameters:
        path:           str - Path to ONNX model
        input_data:     Dict[str, np.ndarray] - Input data for ONNX model
        input_shapes:   Dict[str, Iterable[int]] - Input shapes for ONNX model

    Returns:
        path:           str - Path to simplified ONNX model
    """
    import onnxsim

    model = onnx.load(path)
    model_simp, check = onnxsim.simplify(
        model,
        input_data=input_data,
        test_input_shapes=input_shapes,
    )
    assert check, "Simplified ONNX model could not be validated"
    onnx.checker.check_model(model_simp, full_check=True)
    onnx.save_model(model_simp, path)
    return path


def test_onnx_model(torch_model, ort_session, states):
    """
    Simple test that everything converted correctly

    Parameters:
        torch_model:    torch.nn.Module - Original torch model
        ort_session:    onnxruntime.InferenceSession - Inference Session for converted ONNX model
        input_features: Dict[str, np.ndarray] - Input features
    """
    states_torch = copy.deepcopy(states)
    states_onnx = copy.deepcopy(states)

    for i in range(30):
        input_frame = torch.randn(FRAME_SIZE)
        # torch
        output_torch = torch_model(input_frame, states_torch)

        # onnx
        output_onnx = ort_session.run(
            OUTPUT_NAMES,
            generate_onnx_features([input_frame, states_onnx]),
        )

        for x, y, name in zip(output_torch, output_onnx, OUTPUT_NAMES):
            y_tensor = torch.from_numpy(y)
            assert torch.allclose(
                x, y_tensor, atol=1e-2
            ), f"out {name} - {i}, {x.flatten()[-5:]}, {y_tensor.flatten()[-5:]}"


def generate_onnx_features(input_features):
    return {x: y.detach().cpu().numpy() for x, y in zip(INPUT_NAMES, input_features)}


def perform_benchmark(
    ort_session,
    input_features: Dict[str, np.ndarray],
):
    """
    Benchmark ONNX model performance

    Parameters:
        ort_session:    onnxruntime.InferenceSession - Inference Session for converted ONNX model
        input_features: Dict[str, np.ndarray] - Input features
    """

    def run_onnx():
        output = ort_session.run(
            OUTPUT_NAMES,
            input_features,
        )

    t0 = benchmark.Timer(
        stmt="run_onnx()",
        num_threads=1,
        globals={"run_onnx": run_onnx},
    )
    print(
        f"Median iteration time: {t0.blocked_autorange(min_run_time=10).median * 1e3:6.2f} ms / {480 / 48000 * 1000} ms"
    )


def infer_onnx_model(streaming_pipeline, ort_session, inference_path):
    """
    Inference ONNX model with TorchDFPipeline
    """
    del streaming_pipeline.torch_streaming_model
    streaming_pipeline.torch_streaming_model = lambda *features: (
        torch.from_numpy(x)
        for x in ort_session.run(
            OUTPUT_NAMES,
            generate_onnx_features(list(features)),
        )
    )

    noisy_audio, sr = torchaudio.load(inference_path, channels_first=True)
    noisy_audio = noisy_audio.mean(dim=0).unsqueeze(0)  # stereo to mono

    enhanced_audio = streaming_pipeline(noisy_audio, sr)

    torchaudio.save(
        inference_path.replace(".wav", "_onnx_infer.wav"),
        enhanced_audio,
        sr,
        encoding="PCM_S",
        bits_per_sample=16,
    )


def trace_handler(prof):
    output = prof.key_averages().table(sort_by="cpu_time_total", row_limit=20)
    print(output)
    prof.export_chrome_trace("trace.json")


def main(args):
    streaming_pipeline = TorchDFMinimalPipeline(device="cpu")
    torch_df = streaming_pipeline.torch_streaming_model
    # states = streaming_pipeline.states

    model_parameters = filter(lambda p: p.requires_grad, torch_df.parameters())
    params = sum([np.prod(p.size()) for p in model_parameters])
    print("Number of params:", params // 1e6, "M")

    input_frame = torch.rand(FRAME_SIZE)
    input_features = [input_frame]
    torch_df(*input_features)  # check model

    # def apply_model(model, features):
    #     model(*features)
    # with profile(
    #     activities=[ProfilerActivity.CPU],
    #     schedule=torch.profiler.schedule(wait=1, warmup=1, active=2),
    #     on_trace_ready=trace_handler,
    #     record_shapes=True,
    # ) as prof:
    #     for _ in range(8):
    #         apply_model(torch_df, input_features)
    #         prof.step()

    torch_df_script = torch.jit.script(torch_df)
    torch.onnx.export(
        torch_df_script,
        input_features,
        args.output_path,
        verbose=False,
        input_names=INPUT_NAMES,
        output_names=OUTPUT_NAMES,
        opset_version=14,
        export_params=True,
        do_constant_folding=True,
    )
    print(f"Model exported to {args.output_path}!")

    onnx.checker.check_model(onnx.load(args.output_path), full_check=True)

    print(f"Model {args.output_path} checked!")

    shape_inference.quant_pre_process(
        args.output_path,
        args.output_path,
        skip_symbolic_shape=False,
    )

    print(f"Model preprocessed for quantization - {args.output_path}!")

    quantize_dynamic(args.output_path, args.output_path, weight_type=QuantType.QUInt8)

    print(f"Model quantized - {args.output_path}!")

    input_features_onnx = generate_onnx_features(input_features)
    input_shapes_dict = {x: y.shape for x, y in input_features_onnx.items()}

    # Simplify not working!
    if args.simplify:
        raise NotImplementedError("Simplify not working for flatten states!")
        onnx_simplify(args.output_path, input_features_onnx, input_shapes_dict)
        print(f"Model simplified! {args.output_path}")

    if args.ort:
        if (
            subprocess.run(
                [
                    "python",
                    "-m",
                    "onnxruntime.tools.convert_onnx_models_to_ort",
                    args.output_path,
                    "--optimization_style",
                    "Fixed",
                ]
            ).returncode
            != 0
        ):
            raise RuntimeError("ONNX to ORT conversion failed!")
        print("Model converted to ORT format!")

    print("Checking model...")
    sess_options = ort.SessionOptions()
    sess_options.graph_optimization_level = (
        ort.GraphOptimizationLevel.ORT_ENABLE_EXTENDED
    )
    sess_options.optimized_model_filepath = args.output_path
    sess_options.inter_op_num_threads = 1
    sess_options.intra_op_num_threads = 1
    sess_options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL

    ort_session = ort.InferenceSession(
        args.output_path, sess_options, providers=["CPUExecutionProvider"]
    )

    onnx_outputs = ort_session.run(
        OUTPUT_NAMES,
        input_features_onnx,
    )

    print(
        f"InferenceSession successful! Output shapes: {[x.shape for x in onnx_outputs]}"
    )

    if args.test:
        test_onnx_model(torch_df, ort_session, input_features[1])
        print("Tests passed!")

    if args.performance:
        print("Performanse check...")
        perform_benchmark(ort_session, input_features_onnx)

    if args.inference_path:
        infer_onnx_model(streaming_pipeline, ort_session, args.inference_path)
        print(f"Audio from {args.inference_path} enhanced!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Exporting torchDF model to ONNX")
    parser.add_argument(
        "--output-path",
        type=str,
        default="denoiser_model.onnx",
        help="Path to output onnx file",
    )
    parser.add_argument("--simplify", action="store_true", help="Simplify the model")
    parser.add_argument("--test", action="store_true", help="Test the onnx model")
    parser.add_argument(
        "--performance",
        action="store_true",
        help="Mesure median iteration time for onnx model",
    )
    parser.add_argument("--inference-path", type=str, help="Run inference on example")
    parser.add_argument("--ort", action="store_true", help="Save to ort format")
    parser.add_argument(
        "--always-apply-all-stages", action="store_true", help="Always apply stages"
    )
    main(parser.parse_args())
