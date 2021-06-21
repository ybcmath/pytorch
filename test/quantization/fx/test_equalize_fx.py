import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.quantization import default_qconfig
from torch.quantization.observer import MinMaxObserver, PerChannelMinMaxObserver
from torch.quantization.quantize_fx import prepare_fx, convert_fx
from torch.quantization.fx.convert import run_weight_observers
from torch.quantization.fx._equalize import (
    _InputEqualizationObserver,
    _WeightEqualizationObserver,
    calculate_equalization_scale,
    default_equalization_qconfig,
    _convert_equalization_ref
)

from torch.testing._internal.common_quantization import NodeSpec as ns
from torch.testing._internal.common_quantization import QuantizationTestCase

# Standard Libraries
import copy
import numpy as np

# Testing utils
from hypothesis import given
from hypothesis import strategies as st

# Basic test with one linear layer
class LinearModule(nn.Module):
    def __init__(self):
        super().__init__()
        self.linear = nn.Linear(2, 2)

    def forward(self, x):
        return self.linear(x)

# Test with two connected linear layers
class Linear2Module(nn.Module):
    def __init__(self):
        super().__init__()
        self.linear1 = nn.Linear(2, 2)
        self.linear2 = nn.Linear(2, 2)

    def forward(self, x):
        x = self.linear1(x)
        x = self.linear2(x)
        return x
# Test with two connected linear layers
class Linear2FP32Module(nn.Module):
    def __init__(self):
        super().__init__()
        self.linear1 = nn.Linear(2, 2)
        self.linear2 = nn.Linear(2, 2)

    def forward(self, x):
        x = self.linear1(x)
        x = torch.add(x, 5)
        x = self.linear2(x)
        return x

class Linear(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.weight = torch.rand((2, 2))
        self.bias = torch.zeros(2)

    def forward(self, x):
        return F.linear(x, self.weight, self.bias)

# Test where we have one functional linear layer
class FunctionalLinearModule(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.linear1 = Linear()

    def forward(self, x):
        x = self.linear1(x)
        return x

# Test where we have two connected functional linear layers
class FunctionalLinear2Module(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.linear1 = Linear()
        self.linear2 = Linear()

    def forward(self, x):
        x = self.linear1(x)
        x = self.linear2(x)
        return x

# Test where we have a Linear layer followed by a fp32 operation
# (conv layer) without a qconfig
class FunctionalLinear2FP32Module(torch.nn.Module):
    def __init__(self):
        super().__init__()
        self.linear1 = Linear()
        self.linear2 = Linear()

    def forward(self, x):
        x = self.linear1(x)
        x = torch.add(x, 5)
        x = self.linear2(x)
        return x

qconfig_dict = {"": None,
                "object_type": [(nn.Linear, default_qconfig),
                                (F.linear, default_qconfig)]}

default_equalization_qconfig_dict = {
    "": None,
    "object_type": [(nn.Linear, default_equalization_qconfig),
                    (F.linear, default_equalization_qconfig)]
}


class TestEqualizeFx(QuantizationTestCase):
    @given(input_qdtype=st.sampled_from((torch.qint8, torch.quint8)),
           input_qscheme=st.sampled_from((torch.per_tensor_affine, torch.per_tensor_symmetric)),
           weight_qdtype=st.sampled_from((torch.qint8, torch.quint8)),
           weight_qscheme=st.sampled_from((torch.per_channel_affine, torch.per_channel_symmetric,
                                           torch.per_channel_affine_float_qparams)))
    def test_input_weight_eq_observer(self, input_qdtype, input_qscheme, weight_qdtype, weight_qscheme):
        """ Tests that the Input- and Weight- EqualizationObservers perform as expected
        """

        input_eq_obs = _InputEqualizationObserver(dtype=input_qdtype, qscheme=input_qscheme)
        weight_eq_obs = _WeightEqualizationObserver(dtype=weight_qdtype, qscheme=weight_qscheme)

        width = np.random.randint(1, 10)
        x_height = np.random.randint(2, 10)
        w_height = np.random.randint(2, 10)

        x = (np.random.random(size=(x_height, width)) * 10).round(decimals=2).astype(np.float32)
        w = (np.random.random(size=(w_height, width)) * 10).round(decimals=2).astype(np.float32)

        ret_x = input_eq_obs(torch.tensor(x))
        ret_w = weight_eq_obs(torch.tensor(w))
        self.assertEqual((ret_x, ret_w), (x, w))

        # Check the min/max input columns are correct
        ref_min_inputs = x.min(axis=0)
        ref_max_inputs = x.max(axis=0)
        self.assertEqual(input_eq_obs.get_input_minmax(), (ref_min_inputs, ref_max_inputs))

        # Check the min/max weight columns are correct
        ref_min_weights_col = w.min(axis=0)
        ref_max_weights_col = w.max(axis=0)
        self.assertEqual(weight_eq_obs.get_weight_col_minmax(), (ref_min_weights_col, ref_max_weights_col))

        # Check the equalization scale is correct
        equalization_scale = calculate_equalization_scale(input_eq_obs, weight_eq_obs)
        ref_equalization_scale = np.sqrt((ref_max_weights_col - ref_min_weights_col) /
                                         (ref_max_inputs - ref_min_inputs))
        self.assertEqual(equalization_scale, ref_equalization_scale)

        input_eq_obs.set_equalization_scale(equalization_scale)
        weight_eq_obs.set_equalization_scale(equalization_scale)

        # Check the input scale/zero-point values
        min_input_scaled, max_input_scaled = input_eq_obs.calculate_scaled_minmax()
        input_quant_obs = MinMaxObserver(dtype=input_qdtype, qscheme=input_qscheme)
        input_quant_obs.min_val = min_input_scaled
        input_quant_obs.max_val = max_input_scaled
        input_qparams = input_quant_obs.calculate_qparams()

        ref_min_input_scaled = np.min(ref_min_inputs * ref_equalization_scale)
        ref_min_input_scaled = min(0, ref_min_input_scaled)
        ref_max_input_scaled = np.max(ref_max_inputs * ref_equalization_scale)
        ref_max_input_scaled = max(0, ref_max_input_scaled)

        if input_qscheme == torch.per_tensor_symmetric:
            ref_scale = 2 * max(abs(ref_min_input_scaled), ref_max_input_scaled) / 255
            ref_zero_point = 0 if input_qdtype is torch.qint8 else 128
        else:
            ref_scale = (ref_max_input_scaled - ref_min_input_scaled) / 255
            ref_zero_point = -128 if input_qdtype is torch.qint8 else 0

        self.assertEqual(input_qparams[0].item(), ref_scale, atol=1e-5, rtol=0)
        self.assertEqual(input_qparams[1].item(), ref_zero_point)

        # During input-weight equalization, we will scale the weights so that
        # the following weight quantized observer will have the correct scaled qparams
        # Check the weight scale/zero-point values of the quantized observer
        weight_quant_obs = PerChannelMinMaxObserver(dtype=weight_qdtype, qscheme=weight_qscheme)

        # Scale the weights for input-weight equalization
        ref_w_scaled = w * np.reciprocal(ref_equalization_scale)
        w_scaled = torch.mul(torch.tensor(w), torch.reciprocal(equalization_scale))
        self.assertEqual(ref_w_scaled, w_scaled)

        # Call forward on the weight quantization observer
        weight_quant_obs(w_scaled)

        # Check the min/max weight rows are correct
        ref_min_weights_scaled = ref_w_scaled.min(axis=1)
        ref_max_weights_scaled = ref_w_scaled.max(axis=1)
        self.assertEqual(weight_quant_obs.min_vals, ref_min_weights_scaled)
        self.assertEqual(weight_quant_obs.max_vals, ref_max_weights_scaled)

        weight_qparams = weight_quant_obs.calculate_qparams()

        if weight_qscheme == torch.per_channel_symmetric:
            ref_min_weights_scaled = np.minimum(np.zeros(ref_min_weights_scaled.shape), ref_min_weights_scaled)
            ref_max_weights_scaled = np.maximum(np.zeros(ref_max_weights_scaled.shape), ref_max_weights_scaled)

            ref_scales = 2 * np.maximum(np.abs(ref_min_weights_scaled), ref_max_weights_scaled) / 255
            ref_zero_points = np.zeros_like(
                ref_scales) if weight_qdtype is torch.qint8 else np.ones_like(ref_scales) * 128
        elif weight_qscheme == torch.per_channel_affine_float_qparams:
            ref_scales = (ref_max_weights_scaled - ref_min_weights_scaled) / 255
            ref_scales = np.where(ref_scales > 1e-7, ref_scales, np.ones_like(ref_scales))
            ref_zero_points = -1 * ref_min_weights_scaled / ref_scales
        else:
            ref_min_weights_scaled = np.minimum(np.zeros_like(ref_min_weights_scaled), ref_min_weights_scaled)
            ref_max_weights_scaled = np.maximum(np.zeros_like(ref_max_weights_scaled), ref_max_weights_scaled)

            ref_scales = (ref_max_weights_scaled - ref_min_weights_scaled) / 255
            ref_zero_points = -128 if weight_qdtype is torch.qint8 else 0
            ref_zero_points = ref_zero_points - np.round(ref_min_weights_scaled / ref_scales)

        self.assertTrue(torch.allclose(weight_qparams[0], torch.tensor(
            ref_scales, dtype=weight_qparams[0].dtype), atol=0.0001))
        self.assertTrue(torch.allclose(weight_qparams[1], torch.tensor(
            ref_zero_points, dtype=weight_qparams[1].dtype), atol=1))

    def test_input_weight_equalization_prepare(self):
        """ Tests that graphs created after prepare_fx is as expected
        """

        linear_node_occurrence = {
            ns.call_module(_InputEqualizationObserver): 1,
            ns.call_module(MinMaxObserver): 2,
        }

        linear2_node_occurrence = {
            ns.call_module(_InputEqualizationObserver): 2,
            ns.call_module(MinMaxObserver): 3,
        }

        functionalLinear_node_occurrence = {
            ns.call_module(_InputEqualizationObserver): 2,
            ns.call_module(_WeightEqualizationObserver): 2,
            ns.call_module(MinMaxObserver): 5,
        }

        functionalLinear2FP32_node_occurrence = {
            ns.call_module(_InputEqualizationObserver): 2,
            ns.call_module(_WeightEqualizationObserver): 2,
            ns.call_module(MinMaxObserver): 6,
        }

        tests = [(LinearModule, default_equalization_qconfig_dict, linear_node_occurrence),
                 (Linear2Module, default_equalization_qconfig_dict, linear2_node_occurrence),
                 (FunctionalLinear2Module, default_equalization_qconfig_dict, functionalLinear_node_occurrence),
                 (FunctionalLinear2FP32Module, default_equalization_qconfig_dict, functionalLinear2FP32_node_occurrence)]

        for (M, equalization_qconfig_dict, node_occurrence) in tests:
            m = M().eval()
            prepared = prepare_fx(m, qconfig_dict, equalization_qconfig_dict=equalization_qconfig_dict)
            self.checkGraphModuleNodes(prepared, expected_node_occurrence=node_occurrence)

    def test_input_weight_equalization_convert(self):
        """ Tests that the modified model for equalization (before quantization)
        returns the same output as the original model
        """

        tests = [(LinearModule, default_equalization_qconfig_dict),
                 (Linear2FP32Module, default_equalization_qconfig_dict),
                 (FunctionalLinearModule, default_equalization_qconfig_dict),
                 (FunctionalLinear2FP32Module, default_equalization_qconfig_dict),
                 (Linear2Module, default_equalization_qconfig_dict),
                 (FunctionalLinear2Module, default_equalization_qconfig_dict)]

        x = torch.rand((3, 2))
        for (M, equalization_qconfig_dict) in tests:
            m = M().eval()
            prepared = prepare_fx(m, qconfig_dict, equalization_qconfig_dict=equalization_qconfig_dict)
            output = prepared(x)

            convert_ref = _convert_equalization_ref(prepared)
            convert_ref_output = convert_ref(x)

            m = M().eval()
            prepared = prepare_fx(m, qconfig_dict, equalization_qconfig_dict=equalization_qconfig_dict)
            prepared(x)
            convert_fx(prepared)  # Check if compile
            self.assertEqual(output, convert_ref_output)

    def calculate_equalization_scale_ref(self, x, w):
        """ Calculates the equalization scale based on the input and weight
        """
        min_inputs = x.min(axis=0)
        max_inputs = x.max(axis=0)

        min_weights_col = w.min(axis=0)
        max_weights_col = w.max(axis=0)

        equalization_scale = np.sqrt((max_weights_col - min_weights_col) /
                                     (max_inputs - min_inputs))
        return equalization_scale

    def get_expected_eq_scales(self, model, x):
        """ For each module in the graph, we want to calculate the equalization
        scale at that point
        """
        exp_eq_scales = []
        for _, module in model.named_children():
            weight = module.weight.detach().numpy()
            bias = module.bias.detach().numpy()

            eq_scale = self.calculate_equalization_scale_ref(x, weight)
            exp_eq_scales.append(eq_scale)

            x = x @ weight.T + bias

        return exp_eq_scales

    def test_input_weight_equalization_equalization_scales(self):
        """ After applying the equalization functions, check if the equalization
        scales are the expected values
        """

        tests = [(LinearModule, default_equalization_qconfig_dict),
                 (Linear2Module, default_equalization_qconfig_dict),
                 (FunctionalLinearModule, default_equalization_qconfig_dict),
                 (FunctionalLinear2Module, default_equalization_qconfig_dict)]

        x = torch.rand((3, 2))
        for (M, equalization_qconfig_dict) in tests:
            m = M().eval()
            exp_eq_scales = self.get_expected_eq_scales(m, x.detach().numpy())

            prepared = prepare_fx(m, qconfig_dict, equalization_qconfig_dict=equalization_qconfig_dict)
            prepared(x)
            convert_ref = _convert_equalization_ref(prepared)
            convert_ref(x)

            counter = 0
            for node in convert_ref.graph.nodes:
                if 'equalization_scale' in node.name and node.op == 'get_attr':
                    self.assertEqual(convert_ref.get_buffer(str(node.target)), exp_eq_scales[counter])
                    counter += 1

    def get_expected_weights_bias(self, model, x, exp_eq_scales):
        """ For each module in the graph, we want to calculate the expected
        scaled weight and bias values
        """
        exp_weights = []
        exp_bias = []
        for i, (_, module) in enumerate(model.named_children()):
            weight = module.weight.detach().numpy()
            bias = module.bias.detach().numpy()

            scaled_weight = weight * np.reciprocal(exp_eq_scales[i])
            scaled_bias = bias
            if i + 1 < len(exp_eq_scales):
                scaled_weight = (scaled_weight.T * exp_eq_scales[i + 1]).T
                scaled_bias = (scaled_bias.T * exp_eq_scales[i + 1]).T

            exp_weights.append(scaled_weight)
            exp_bias.append(scaled_bias)

            x = x @ weight.T + bias

        return exp_weights, exp_bias

    def test_input_weight_equalization_weights_bias(self):
        """ After applying the equalization functions check if the weights and
        biases are as expected
        """

        tests = [(LinearModule, default_equalization_qconfig_dict),
                 (Linear2Module, default_equalization_qconfig_dict),
                 (FunctionalLinearModule, default_equalization_qconfig_dict),
                 (FunctionalLinear2Module, default_equalization_qconfig_dict)]

        x = torch.rand((3, 2))
        for (M, equalization_qconfig_dict) in tests:
            m = M().eval()
            exp_eq_scales = self.get_expected_eq_scales(m, x.detach().numpy())
            exp_weights, exp_bias = self.get_expected_weights_bias(m, x.detach().numpy(), exp_eq_scales)

            prepared = prepare_fx(m, qconfig_dict, equalization_qconfig_dict=equalization_qconfig_dict)
            prepared(x)
            convert_ref = _convert_equalization_ref(prepared)
            convert_ref(x)

            modules = dict(convert_ref.named_modules(remove_duplicate=False))
            counter = 0
            for node in convert_ref.graph.nodes:
                if node.op == 'call_module' and isinstance(modules[str(node.target)], nn.Linear):
                    self.assertEqual(modules[str(node.target)].weight, exp_weights[counter])
                    self.assertEqual(modules[str(node.target)].bias, exp_bias[counter])
                    counter += 1

    def get_expected_inp_act_vals(self, model, x, exp_eq_scales, exp_weights, exp_bias):
        """ For each module in the graph, we want to calculate the expected
        min/max values for every input activation node
        """
        x = x * exp_eq_scales[0]

        exp_inp_activation_vals = []
        for i, _ in enumerate(model.named_children()):
            exp_inp_activation_vals.append((x.min(), x.max()))
            x = x @ exp_weights[i].T + exp_bias[i]

        exp_inp_activation_vals.append((x.min(), x.max()))
        return exp_inp_activation_vals

    def get_expected_weight_act_vals(self, exp_weights):
        """ For each module in the graph, we want to calculate the expected
        min/max values for every weight activation node. This is assuming that
        the weight observers are all MinMaxObservers.
        """

        exp_weight_activation_vals = []
        for w in exp_weights:
            exp_weight_activation_vals.append((w.min(), w.max()))

        return exp_weight_activation_vals

    def test_input_weight_equalization_activation_values(self):
        """ After applying the equalization functions check if each observer's
        min/max values are as expected
        """

        tests = [(LinearModule, default_equalization_qconfig_dict),
                 (Linear2Module, default_equalization_qconfig_dict),
                 (FunctionalLinearModule, default_equalization_qconfig_dict),
                 (FunctionalLinear2Module, default_equalization_qconfig_dict)]

        x = torch.rand((3, 2))
        for (M, equalization_qconfig_dict) in tests:
            m = M().eval()
            exp_eq_scales = self.get_expected_eq_scales(m, x.detach().numpy())
            exp_weights, exp_bias = self.get_expected_weights_bias(m, x.detach().numpy(), exp_eq_scales)
            exp_inp_act_vals = self.get_expected_inp_act_vals(m, x, exp_eq_scales, exp_weights, exp_bias)
            exp_weight_act_vals = self.get_expected_weight_act_vals(exp_weights)

            prepared = prepare_fx(m, qconfig_dict, equalization_qconfig_dict=equalization_qconfig_dict)
            prepared(x)
            convert_ref = _convert_equalization_ref(prepared)
            run_weight_observers(convert_ref)
            convert_ref(x)

            modules = dict(convert_ref.named_modules(remove_duplicate=False))
            inp_counter = 0
            weight_counter = 0
            for node in convert_ref.graph.nodes:
                if "weight" not in node.name and node.op == 'call_module' and \
                   isinstance(modules[str(node.target)], MinMaxObserver):
                    # Check min/max values of input activation layers
                    exp_min_val, exp_max_val = exp_inp_act_vals[inp_counter]
                    self.assertEqual(modules[str(node.target)].min_val, exp_min_val)
                    self.assertEqual(modules[str(node.target)].max_val, exp_max_val)
                    inp_counter += 1

                elif node.op == 'call_module' and isinstance(modules[str(node.target)], MinMaxObserver):
                    # Check min/max values of weight activation layers
                    assert("weight" in node.name)
                    exp_min_val, exp_max_val = exp_weight_act_vals[weight_counter]
                    self.assertEqual(modules[str(node.target)].min_val, exp_min_val)
                    self.assertEqual(modules[str(node.target)].max_val, exp_max_val)
                    weight_counter += 1

    def check_orig_and_eq_graphs(self, orig_model, eq_model):
        """ Given a non-equalized model and an equalized model, check that the
        graphs are structured in the same way, except the equalized model has
        additional 'equalization_scale' and 'mul' nodes.
        """
        orig_idx = 0
        orig_nodes = list(orig_model.graph.nodes)
        orig_modules = dict(orig_model.named_modules(remove_duplicate=False))

        eq_idx = 0
        eq_nodes = list(eq_model.graph.nodes)
        eq_modules = dict(eq_model.named_modules(remove_duplicate=False))

        while orig_idx < len(orig_nodes) and eq_idx < len(eq_nodes):
            if 'equalization_scale' in eq_nodes[eq_idx].name and 'mul' in eq_nodes[eq_idx + 1].name:
                # Skip the equalization and mul nodes
                eq_idx += 2
                continue
            elif orig_nodes[orig_idx].op != eq_nodes[eq_idx].op:
                return False
            elif orig_nodes[orig_idx].op == 'call_module':
                # Check that the type of call_modules are the same (ex. nn.Linear, MinMaxObserver)
                orig_node = orig_nodes[orig_idx]
                eq_node = eq_nodes[eq_idx]
                if type(orig_modules[orig_node.target]) is not type(eq_modules[eq_node.target]):
                    return False
            elif orig_nodes[orig_idx].op == 'call_function':
                # Check that the call_functions are the same (ex. F.linear)
                orig_node = orig_nodes[orig_idx]
                eq_node = eq_nodes[eq_idx]
                if orig_node.target != eq_node.target:
                    return False

            eq_idx += 1
            orig_idx += 1

        return True

    def test_input_weight_equalization_graphs(self):
        """ Tests that the modified model for equalization has the same graph
        structure as the model without equalization (before and after
        quantization).
        """

        tests = [(LinearModule, default_equalization_qconfig_dict),
                 (Linear2FP32Module, default_equalization_qconfig_dict),
                 (FunctionalLinearModule, default_equalization_qconfig_dict),
                 (FunctionalLinear2FP32Module, default_equalization_qconfig_dict),
                 (Linear2Module, default_equalization_qconfig_dict),
                 (FunctionalLinear2Module, default_equalization_qconfig_dict)]

        torch.manual_seed(0)
        x = torch.rand((3, 2))
        for (M, equalization_qconfig_dict) in tests:
            # No equalization
            m = M().eval()
            prepared = prepare_fx(m, qconfig_dict)
            prepared_model = copy.deepcopy(prepared)
            prepared(x)
            quantized_model = convert_fx(prepared)

            # Equalization
            m = M().eval()
            prepared = prepare_fx(m, qconfig_dict, equalization_qconfig_dict=equalization_qconfig_dict)
            prepared(x)
            equalized_model = _convert_equalization_ref(prepared)

            # Check that the graph before quantization is pretty much the same
            self.assertTrue(self.check_orig_and_eq_graphs(prepared_model, equalized_model))

            m = M().eval()
            prepared = prepare_fx(m, qconfig_dict, equalization_qconfig_dict=equalization_qconfig_dict)
            prepared(x)
            equalized_quantized_model = convert_fx(prepared)

            # Check that the graph after quantization is pretty much the same
            self.assertTrue(self.check_orig_and_eq_graphs(quantized_model, equalized_quantized_model))
