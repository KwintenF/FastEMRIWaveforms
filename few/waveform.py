# Main waveform class location

# Copyright (C) 2020 Michael L. Katz, Alvin J.K. Chua, Niels Warburton, Scott A. Hughes
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import sys
import os
from abc import ABC

import numpy as np
from tqdm import tqdm

# check if cupy is available / GPU is available
try:
    import cupy as xp

except (ImportError, ModuleNotFoundError) as e:
    import numpy as xp


from few.utils.baseclasses import SchwarzschildEccentric
from few.trajectory.flux import RunSchwarzEccFluxInspiral
from few.amplitude.interp2dcubicspline import Interp2DAmplitude
from few.utils.overlap import get_mismatch
from few.amplitude.romannet import RomanAmplitude
from few.utils.modeselector import ModeSelector
from few.utils.ylm import GetYlms
from few.summation.directmodesum import DirectModeSum
from few.utils.constants import *
from few.utils.citations import *
from few.summation.interpolatedmodesum import InterpolatedModeSum

# Longer Term
# TODO: run trajectory backward
# TODO: add initial phases
# TODO: zero out modes
# TODO: shared memory based on CUDA_ARCH / upping shared allocation
# TODO: deal with file locations and removing files from git history
# TODO: add tutorials to documentation
# TODO: general waveform base class
# TODO: more automatic/generic download from zenodo based on versioning
# TODO: add benchmark test

# Shorter Term
# TODO: document in line / check / cleanup
# TODO: free memory in amplitudes


class SchwarzschildEccentricWaveformBase(SchwarzschildEccentric, ABC):
    """Base class for the actual Schwarzschild eccentric waveforms.

    This class carries information and methods that are common to any
    implementation of Schwarzschild eccentric waveforms. These include
    initialization and the actual base code for building a waveform. This base
    code calls the various modules chosen by the user or according to the
    predefined waveform classes available. See
    :class:`few.utils.baseclasses.SchwarzschildEccentric` for information
    high level information on these waveform models.

    args:
        inspiral_module (obj): Class object representing the module
            for creating the inspiral. This returns the phases and orbital
            parameters. See :ref:`trajectory-label`.
        amplitude_module (obj): Class object representing the module for
            generating amplitudes. See :ref:`amplitude-label` for more
            information.
        sum_module (obj): Class object representing the module for summing the
            final waveform from the amplitude and phase information. See
            :ref:`summation-label`.
        inspiral_kwargs (dict, optional): Optional kwargs to pass to the
            inspiral generator. **Important Note**: These kwargs are passed
            online, not during instantiation like other kwargs here. Default is
            {}.
        amplitude_kwargs (dict, optional): Optional kwargs to pass to the
            amplitude generator during instantiation. Default is {}.
        sum_kwargs (dict, optional): Optional kwargs to pass to the
            sum module during instantiation. Default is {}.
        Ylm_kwargs (dict, optional): Optional kwargs to pass to the
            Ylm generator during instantiation. Default is {}.
        use_gpu (bool, optional): If True, use GPU resources. Default is False.
        normalize_amps (bool, optional): If True, it will normalize amplitudes
            to flux information output from the trajectory modules. Default
            is True.

    """

    def attributes_SchwarzschildEccentricWaveformBase(self):
        """
        attributes:
            inspiral_generator (obj): instantiated trajectory module.
            amplitude_generator (obj): instantiated amplitude module.
            create_waveform (obj): instantiated summation module.
            ylm_gen (obj): instantiated Ylm module.
            num_teuk_modes (int): number of Teukolsky modes in the model.
            ls, ms, ns (1D int xp.ndarray): Arrays of mode indices :math:`(l,m,n)`
                after filtering operation. If no filtering, these are equivalent
                to l_arr, m_arr, n_arr.

        """
        pass

    def __init__(
        self,
        inspiral_module,
        amplitude_module,
        sum_module,
        inspiral_kwargs={},
        amplitude_kwargs={},
        sum_kwargs={},
        Ylm_kwargs={},
        use_gpu=False,
        normalize_amps=True,
    ):

        self.sanity_check_gpu(use_gpu)

        SchwarzschildEccentric.__init__(self, use_gpu)

        if use_gpu:
            self.xp = xp

        else:
            self.xp = np

        self.normalize_amps = normalize_amps

        self.inspiral_kwargs = inspiral_kwargs
        self.inspiral_generator = inspiral_module()

        self.amplitude_generator = amplitude_module(**amplitude_kwargs)
        self.create_waveform = sum_module(**sum_kwargs)

        self.ylm_gen = GetYlms(use_gpu=use_gpu, **Ylm_kwargs)

        self.mode_selector = ModeSelector(self.m0mask, use_gpu=use_gpu)

    @classmethod
    @property
    def gpu_capability(self):
        raise NotImplementedError

    @classmethod
    @property
    def allow_batching(self):
        return NotImplementedError

    @property
    def citation(self):
        return few_citation + romannet_citation

    def __call__(
        self,
        M,
        mu,
        p0,
        e0,
        theta,
        phi,
        dt=10.0,
        T=1.0,
        eps=1e-5,
        show_progress=False,
        batch_size=-1,
        mode_selection=None,
    ):
        """Call function for SchwarzschildEccentric models.

        This function will take input parameters and produce Schwarzschild
        eccentric waveforms. It will use all of the modules preloaded to
        compute desired outputs.

        args:
            M (double): Mass of larger black hole in solar masses.
            mu (double): Mass of compact object in solar masses.
            p0 (double): Initial semilatus rectum (:math:`10\leq p_0\leq16 + e_0`).
                See documentation for more information on :math:`p_0<10`.
            e0 (double): Initial eccentricity (:math:`0.0\leq e_0\leq0.7`).
            theta (double): Polar viewing angle (:math:`-\pi/2\leq\Theta\leq\pi/2`).
            phi (double): Azimuthal viewing angle.
            dt (double, optional): Time between samples in seconds (inverse of
                sampling frequency). Default is 10.0.
            T (double, optional): Total observation time in years.
                Default is 1.0.
            eps (double, optional): Controls the fractional accuracy during mode
                filtering. Raising this parameter will remove modes. Lowering
                this parameter will add modes. Default that gives a good overalp
                is 1e-5.
            show_progress (bool, optional): If True, show progress through
                amplitude/waveform batches using
                `tqdm <https://tqdm.github.io/>`_. Default is False.
            batch_size (int, optional): If less than 0, create the waveform
                without batching. If greater than zero, create the waveform
                batching in sizes of batch_size. Default is -1.
            mode_selection (str or list or None): Determines the type of mode
                filtering to perform. If None, perform our base mode filtering
                with eps as the fractional accuracy on the total power.
                If 'all', it will run all modes without filtering. If a list of
                tuples (or lists) of mode indices
                (e.g. [(:math:`l_1,m_1,n_1`), (:math:`l_2,m_2,n_2`)]) is
                provided, it will return those modes combined into a
                single waveform.

        Returns:
            1D complex128 xp.ndarray: The output waveform.

        """

        theta, phi = self.sanity_check_viewing_angles(theta, phi)
        self.sanity_check_init(M, mu, p0, e0)
        Tsec = T * YRSID_SI

        # get trajectory
        (t, p, e, Phi_phi, Phi_r, amp_norm) = self.inspiral_generator(
            M, mu, p0, e0, T=T, dt=dt, **self.inspiral_kwargs
        )

        self.sanity_check_traj(p, e)

        self.plunge_time = t[-1]
        # convert for gpu
        t = self.xp.asarray(t)
        p = self.xp.asarray(p)
        e = self.xp.asarray(e)
        Phi_phi = self.xp.asarray(Phi_phi)
        Phi_r = self.xp.asarray(Phi_r)
        amp_norm = self.xp.asarray(amp_norm)

        ylms = self.ylm_gen(self.unique_l, self.unique_m, theta, phi).copy()[
            self.inverse_lm
        ]

        # split into batches

        if batch_size == -1 or self.allow_batching is False:
            inds_split_all = [self.xp.arange(len(t))]
        else:
            split_inds = []
            i = 0
            while i < len(t):
                i += batch_size
                if i >= len(t):
                    break
                split_inds.append(i)

            inds_split_all = self.xp.split(self.xp.arange(len(t)), split_inds)

        iterator = enumerate(inds_split_all)
        iterator = tqdm(iterator, desc="time batch") if show_progress else iterator

        if show_progress:
            print("total:", len(inds_split_all))

        for i, inds_in in iterator:

            t_temp = t[inds_in]
            p_temp = p[inds_in]
            e_temp = e[inds_in]
            Phi_phi_temp = Phi_phi[inds_in]
            Phi_r_temp = Phi_r[inds_in]
            amp_norm_temp = amp_norm[inds_in]

            # amplitudes
            teuk_modes = self.amplitude_generator(
                p_temp, e_temp, self.l_arr, self.m_arr, self.n_arr
            )

            if self.normalize_amps:
                amp_for_norm = self.xp.sum(
                    self.xp.abs(
                        self.xp.concatenate(
                            [teuk_modes, self.xp.conj(teuk_modes[:, self.m0mask])],
                            axis=1,
                        )
                    )
                    ** 2,
                    axis=1,
                ) ** (1 / 2)

                factor = amp_norm_temp / amp_for_norm
                teuk_modes = teuk_modes * factor[:, np.newaxis]

            if isinstance(mode_selection, str):
                if mode_selection == "all":
                    self.ls = self.l_arr[: teuk_modes.shape[1]]
                    self.ms = self.m_arr[: teuk_modes.shape[1]]
                    self.ns = self.n_arr[: teuk_modes.shape[1]]

                    keep_modes = self.xp.arange(teuk_modes.shape[1])
                    temp2 = keep_modes * (keep_modes < self.num_m0) + (
                        keep_modes + self.num_m_1_up
                    ) * (keep_modes >= self.num_m0)

                    ylmkeep = self.xp.concatenate([keep_modes, temp2])
                    ylms_in = ylms[ylmkeep]
                    teuk_modes_in = teuk_modes

                else:
                    raise ValueError("If mode selection is a string, must be `all`.")

            elif isinstance(mode_selection, list):
                if mode_selection == []:
                    raise ValueError("If mode selection is a list, cannot be empty.")

                keep_modes = self.xp.zeros(len(mode_selection), dtype=self.xp.int32)
                for jj, lmn in enumerate(mode_selection):
                    keep_modes[jj] = self.xp.int32(self.lmn_indices[tuple(lmn)])

                self.ls = self.l_arr[keep_modes]
                self.ms = self.m_arr[keep_modes]
                self.ns = self.n_arr[keep_modes]

                temp2 = keep_modes * (keep_modes < self.num_m0) + (
                    keep_modes + self.num_m_1_up
                ) * (keep_modes >= self.num_m0)

                ylmkeep = self.xp.concatenate([keep_modes, temp2])
                ylms_in = ylms[ylmkeep]
                teuk_modes_in = teuk_modes[:, keep_modes]

            else:
                modeinds = [self.l_arr, self.m_arr, self.n_arr]
                (
                    teuk_modes_in,
                    ylms_in,
                    self.ls,
                    self.ms,
                    self.ns,
                ) = self.mode_selector(teuk_modes, ylms, modeinds, eps=eps)

            self.num_modes_kept = teuk_modes_in.shape[1]

            waveform_temp = self.create_waveform(
                t_temp,
                teuk_modes_in,
                ylms_in,
                dt,
                Tsec,
                Phi_phi_temp,
                Phi_r_temp,
                self.ms,
                self.ns,
            )

            if i > 0:
                waveform = self.xp.concatenate([waveform, waveform_temp])

            else:
                waveform = waveform_temp

        return waveform

    def sanity_check_gpu(self, use_gpu):
        if self.gpu_capability is False and use_gpu is True:
            raise Exception(
                "The use_gpu kwarg is True, but this class does not have GPU capabilites."
            )


class FastSchwarzschildEccentricFlux(SchwarzschildEccentricWaveformBase):
    """Prebuilt model for fast Schwarzschild eccentric flux-based waveforms.

    This model combines the most efficient modules to produce the fastest
    accurate EMRI waveforms. It leverages GPU hardware for maximal acceleration,
    but is also available on for CPUs. Please see
    :class:`few.utils.baseclasses.SchwarzschildEccentric` for general
    information on this class of models.

    The trajectory module used here is :class:`few.trajectory.flux` for a
    flux-based, sparse trajectory. This returns approximately 100 points.

    The amplitudes are then determined with
    :class:`few.amplitude.romannet.RomanAmplitude` along these sparse
    trajectories. This gives complex amplitudes for all modes in this model at
    each point in the trajectory. These are then filtered with
    :class:`few.utils.modeselector.ModeSelector`.

    The modes that make it through the filter are then summed by
    :class:`few.summation.interpolatedmodesum.InterpolatedModeSum`.

    See :class:`few.waveform.SchwarzschildEccentricWaveformBase` for information
    on inputs. See examples as well.

    args:
        inspiral_kwargs (dict, optional): Optional kwargs to pass to the
            inspiral generator. **Important Note**: These kwargs are passed
            online, not during instantiation like other kwargs here. Default is
            {}.
        amplitude_kwargs (dict, optional): Optional kwargs to pass to the
            amplitude generator during instantiation. Default is {}.
        sum_kwargs (dict, optional): Optional kwargs to pass to the
            sum module during instantiation. Default is {}.
        Ylm_kwargs (dict, optional): Optional kwargs to pass to the
            Ylm generator during instantiation. Default is {}.
        use_gpu (bool, optional): If True, use GPU resources. Default is False.
        *args (list, placeholder): args for waveform model.
        **kwargs (dict, placeholder): kwargs for waveform model.

    """

    def __init__(
        self,
        inspiral_kwargs={},
        amplitude_kwargs={},
        sum_kwargs={},
        Ylm_kwargs={},
        use_gpu=False,
        *args,
        **kwargs
    ):

        SchwarzschildEccentricWaveformBase.__init__(
            self,
            RunSchwarzEccFluxInspiral,
            RomanAmplitude,
            InterpolatedModeSum,
            inspiral_kwargs=inspiral_kwargs,
            amplitude_kwargs=amplitude_kwargs,
            sum_kwargs=sum_kwargs,
            Ylm_kwargs=Ylm_kwargs,
            use_gpu=use_gpu,
            *args,
            **kwargs
        )

    def attributes_FastSchwarzschildEccentricFlux(self):
        """
        Attributes:
            gpu_capability (bool): If True, this wavefrom can leverage gpu
                resources. For this class it is True.
            allow_batching (bool): If True, this waveform can use the batch_size
                kwarg. For this class it is False.

        """
        pass

    @property
    def gpu_capability(self):
        return True

    @property
    def allow_batching(self):
        return False


class SlowSchwarzschildEccentricFlux(SchwarzschildEccentricWaveformBase):
    """Prebuilt model for slow Schwarzschild eccentric flux-based waveforms.

    This model combines the various modules to produce the a reference waveform
    against which we test our fast models. Please see
    :class:`few.utils.baseclasses.SchwarzschildEccentric` for general
    information on this class of models.

    The trajectory module used here is :class:`few.trajectory.flux` for a
    flux-based trajectory. For this slow waveform, the DENSE_SAMPLING parameter
    from :class:`few.utils.baseclasses.TrajectoryBase` is fixed to 1 to create
    a densely sampled trajectory.

    The amplitudes are then determined with
    :class:`few.amplitude.interp2dcubicspline.Interp2DAmplitude`
    along a densely sampled trajectory. This gives complex amplitudes
    for all modes in this model at each point in the trajectory. These, can be
    chosent to be filtered, but for reference waveforms, they should not be.

    The modes that make it through the filter are then summed by
    :class:`few.summation.directmodesum.DirectModeSum`.

    See :class:`few.waveform.SchwarzschildEccentricWaveformBase` for information
    on inputs. See examples as well.

    args:
        inspiral_kwargs (dict, optional): Optional kwargs to pass to the
            inspiral generator. **Important Note**: These kwargs are passed
            online, not during instantiation like other kwargs here. Default is
            {}.
        amplitude_kwargs (dict, optional): Optional kwargs to pass to the
            amplitude generator during instantiation. Default is {}.
        sum_kwargs (dict, optional): Optional kwargs to pass to the
            sum module during instantiation. Default is {}.
        Ylm_kwargs (dict, optional): Optional kwargs to pass to the
            Ylm generator during instantiation. Default is {}.
        use_gpu (bool, optional): If True, use GPU resources. Default is False.
        *args (list, placeholder): args for waveform model.
        **kwargs (dict, placeholder): kwargs for waveform model.

    """

    @property
    def gpu_capability(self):
        return False

    @property
    def allow_batching(self):
        return True

    def attributes_SlowSchwarzschildEccentricFlux(self):
        """
        attributes:
            gpu_capability (bool): If True, this wavefrom can leverage gpu
                resources. For this class it is False.
            allow_batching (bool): If True, this waveform can use the batch_size
                kwarg. For this class it is True.
        """
        pass

    def __init__(
        self,
        inspiral_kwargs={},
        amplitude_kwargs={},
        sum_kwargs={},
        Ylm_kwargs={},
        use_gpu=False,
        *args,
        **kwargs
    ):

        # declare specific properties
        inspiral_kwargs["DENSE_STEPPING"] = 1

        SchwarzschildEccentricWaveformBase.__init__(
            self,
            RunSchwarzEccFluxInspiral,
            Interp2DAmplitude,
            DirectModeSum,
            inspiral_kwargs=inspiral_kwargs,
            amplitude_kwargs=amplitude_kwargs,
            sum_kwargs=sum_kwargs,
            Ylm_kwargs=Ylm_kwargs,
            use_gpu=use_gpu,
            *args,
            **kwargs
        )


if __name__ == "__main__":
    import time

    use_gpu = False
    few = FastSchwarzschildEccentricFlux(
        inspiral_kwargs={"DENSE_STEPPING": 0, "max_init_len": int(1e3)},
        amplitude_kwargs={"max_init_len": int(1e3), "use_gpu": use_gpu},
        # amplitude_kwargs=dict(),
        Ylm_kwargs={"assume_positive_m": False},
        sum_kwargs={"use_gpu": use_gpu},
        use_gpu=use_gpu,
    )

    few2 = SlowSchwarzschildEccentricFlux(
        inspiral_kwargs={"DENSE_STEPPING": 1, "max_init_len": int(1e7)},
        # amplitude_kwargs={"max_init_len": int(1e3), "use_gpu": use_gpu},
        amplitude_kwargs=dict(),
        Ylm_kwargs={"assume_positive_m": False},
        sum_kwargs={"use_gpu": True},
        use_gpu=False,
    )

    M = 1e6
    mu = 1e1
    p0 = 10.0
    e0 = 0.7
    theta = np.pi / 2
    phi = 0.0
    dt = 10.0
    T = 1.0  #  / 100.0  # 1124936.040602 / YRSID_SI
    mode_selection = None
    show_progress = True
    batch_size = 10000

    mismatch_out = []
    num_modes = []
    timing = []
    timing_slow = []
    pt_arr = []
    eps_all = 10.0 ** np.arange(-10, -2)

    eps_all = np.concatenate([np.array([1e-25]), eps_all])
    eps_all = np.array([1e-5])

    p0_arr, e0_arr = np.array(
        [
            [10.0, 0.7],
            [11.48, 0.7],
            [12.96, 0.7],
            [14.44, 0.7],
            [15.92, 0.7],
            [17.4, 0.7],
            [16.2, 0.1],
            [16.4, 0.2],
            [16.6, 0.3],
            [16.8, 0.4],
            [17.0, 0.5],
            [17.2, 0.6],
        ]
    ).T

    mu_arr = np.array(
        [
            14.72882724,
            36.69142378,
            72.25349492,
            125.63166025,
            201.04964163,
            304.42722121,
            169.1329517,
            179.99285068,
            192.8791508,
            208.122157,
            229.27693129,
            257.87628876,
        ]
    )
    """
    try:
        fullwave = np.genfromtxt("/projects/b1095/mkatz/emri/slow_1e6_1e1_10_07.txt")
    except OSError:
        fullwave = np.genfromtxt("slow_1e6_1e1_10_07.txt")

    if use_gpu:
        fullwave = xp.asarray(fullwave[:, 5] + 1j * fullwave[:, 6])
    else:
        fullwave = np.asarray(fullwave[:, 5] + 1j * fullwave[:, 6])
    """

    eps = 1e-5
    # for i, eps in enumerate(eps_all):
    for i, (p0, e0, mu) in enumerate(zip(p0_arr, e0_arr, mu_arr)):
        all_modes = False if i > 0 else True
        num = 1
        st = time.perf_counter()
        for jjj in range(num):

            # print(jjj, "\n")
            wc = few(
                M,
                mu,
                p0,
                e0,
                theta,
                phi,
                dt=dt,
                T=T,
                eps=eps,
                mode_selection=mode_selection,
                show_progress=show_progress,
                batch_size=batch_size,
            )
            print(jjj)

        et = time.perf_counter()
        pt = few.plunge_time
        pt_arr.append(pt)

        fast_time = (et - st) / num
        timing.append(fast_time)

        st = time.perf_counter()
        wc2 = few2(
            M,
            mu,
            p0,
            e0,
            theta,
            phi,
            dt=dt,
            T=T,
            eps=eps,
            mode_selection="all",
            show_progress=show_progress,
            batch_size=batch_size,
        )
        et = time.perf_counter()
        timing_slow.append((et - st))
        slow_time = et - st

        try:
            wc = wc.get()
        except AttributeError:
            pass

        try:
            wc2 = wc2.get()
        except AttributeError:
            pass

        min_len = np.min([len(wc), len(wc2)])

        np.save(
            "wave_out_p_{}_e{}".format(p0, e0), np.array([wc[:min_len], wc2[:min_len]])
        )

        exit()

        mm = get_mismatch(wc2, wc, use_gpu=False)
        mismatch_out.append(mm)
        num_modes.append(few.num_modes_kept)

        print(
            "eps:",
            eps,
            "Mismatch:",
            mm,
            "Num modes:",
            few.num_modes_kept,
            "timing fast:",
            fast_time,
            "timing slow:",
            slow_time,
            "plunge_time",
            pt,
        )

    np.save(
        "plot_test_2",
        np.asarray(
            [
                p0_arr,
                e0_arr,
                mu_arr,
                mismatch_out,
                num_modes,
                timing,
                timing_slow,
                pt_arr,
            ]
        ).T,
    )

    """
    num = 20
    st = time.perf_counter()
    for _ in range(num):
        check = few(M, mu, p0, e0, theta, phi, dt=dt, T=T, eps=eps, all_modes=all_modes)
    et = time.perf_counter()

    import pdb

    pdb.set_trace()
    """
    # print(check.shape)
    print((et - st) / num)
