# closure.py
# a general interferometric imager class
#
#    Copyright (C) 2018 Andrew Chael
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU General Public License as published by
#    the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU General Public License
#    along with this program.  If not, see <http://www.gnu.org/licenses/>.


from __future__ import division
from __future__ import print_function
from builtins import str
from builtins import object

import numpy as np
import matplotlib.pyplot as plt
import time

import ehtim.observing.pulses
import ehtim.scattering as so

from ehtim.imaging.imager_utils import *
from ehtim.const_def import *
from ehtim.observing.obs_helpers import *

NHIST = 50 # number of steps to store for hessian approx
STOP = 1e-6 # convergence criterion

DATATERMS = ['vis', 'bs', 'amp', 'cphase', 'camp', 'logcamp']
REGULARIZERS = ['gs', 'tv', 'tv2','l1', 'patch', 'simple', 'flux','cm','compact','compact2']

GRIDDER_P_RAD_DEFAULT = 2
GRIDDER_CONV_FUNC_DEFAULT = 'gaussian'
FFT_PAD_DEFAULT = 2
FFT_INTERP_DEFAULT = 3

###########################################################################################################################################
#Imager object
###########################################################################################################################################
class Imager(object):
    """A general interferometric imager.
    """

    def __init__(self, obsdata, init_im, prior_im=None, flux=None, clipfloor=0., maxit=50, 
                       transform='log', ttype='fast', data_term={'vis':100}, reg_term={'simple':1}, 
                       scattering_model=None, alpha_phi=1e4, systematic_noise=0.0,
                       fft_pad_factor=FFT_PAD_DEFAULT, fft_interp_order=FFT_INTERP_DEFAULT, 
                       fft_conv_func=GRIDDER_CONV_FUNC_DEFAULT, fft_gridder_prad=GRIDDER_P_RAD_DEFAULT):

        self.logstr = ""
        self._obs_list = []
        self._init_list = []
        self._prior_list = []
        self._out_list = []
        self._out_list_epsilon   = []
        self._out_list_scattered = []
        self._flux_list = {}
        self._reg_term_list = []
        self._dat_term_list = []
        self._clipfloor_list = []
        self._maxit_list = []
        self._flux_list = []
        self._transform_list = []
        self._ttype_list = []

        # Parameters for the next imaging iteration
        self.reg_term_next = reg_term #e.g. [('simple',1), ('l1',10), ('flux',500), ('cm',500)]
        self.dat_term_next = data_term #e.g. [('amp', 1000), ('cphase',100)]
        self.systematic_noise = systematic_noise

        self.obs_next = obsdata
        self.init_next = init_im

        self.epsilon_list_next = []

        if prior_im==None:
            self.prior_next = self.init_next
        else:
            self.prior_next = prior_im

        if flux==None:
            self.flux_next = self.prior_next.total_flux()
        else:
            self.flux_next = flux

        # FFT parameters
        self.ttype_next = ttype
        self.fft_gridder_prad = fft_gridder_prad
        self.fft_conv_func = fft_conv_func
        self.fft_pad_factor = fft_pad_factor
        self.fft_interp_order = fft_interp_order

        # Parameters related to scattering
        self.scattering_model = scattering_model
        self._sqrtQ = None
        self._ea_ker = None
        self._ea_ker_gradient_x = None
        self._ea_ker_gradient_y = None
        self._alpha_phi_list = []
        self.alpha_phi_next = alpha_phi

        self.clipfloor_next = clipfloor
        self.maxit_next = maxit
        self.transform_next = transform
        self._change_imgr_params = True
        self.nruns = 0


        #closure amplitude debiasing/snr cut
        self.debias=True
        self.camp_snrcut=0

        #set embedding matrices and prepare imager
        self.check_params()
        self.check_limits()
        self.init_imager_I()


    def set_embed(self):
        """Set embedding matrix.
        """


        self._embed_mask = self.prior_next.imvec > self.clipfloor_next
        coord = np.array([[[x,y] for x in np.arange(self.prior_next.xdim//2,-self.prior_next.xdim//2,-1)]
                                 for y in np.arange(self.prior_next.ydim//2,-self.prior_next.ydim//2,-1)])
        coord = self.prior_next.psize * coord.reshape(self.prior_next.ydim * self.prior_next.xdim, 2)
        self._coord_matrix = coord[self._embed_mask]

        return


    def check_params(self):
        """Check parameter consistency.
        """

        dt_here = False
        dt_type = True

        for term in sorted(self.dat_term_next.keys()):
            if (term != None) and (term != False): dt_here = True
            if not ((term in DATATERMS) or term==False): dt_type = False

        st_here = False
        st_type = True
        for term in sorted(self.reg_term_next.keys()):
            if (term != None) and (term != False): st_here = True
            if not ((term in REGULARIZERS) or term == False): st_type = False

        if not dt_here:
            raise Exception("Must have at least one data term!")

        if not st_here:
            raise Exception("Must have at least one regularizer term!")

        if not dt_type:
            raise Exception("Invalid data term: valid data terms are: " + string.join(DATATERMS))

        if not st_type:
            raise Exception("Invalid regularizer: valid regularizers are: " + string.join(REGULARIZERS))

        if ((self.prior_next.psize != self.init_next.psize) or
            (self.prior_next.xdim != self.init_next.xdim) or
            (self.prior_next.ydim != self.prior_next.ydim)):
            raise Exception("Initial image does not match dimensions of the prior image!")
    
        if self.ttype_next not in ['fast','direct','nfft']:
            raise Exception("Possible ttype_next values are 'fast', 'direct','nfft'!")

        # determine if we need to change the saved imager parameters on the next imager run
        if self.nruns == 0:
            return

        if self.ttype_next != self.ttype_last():
            self._change_imgr_params = True
            return

        if len(self.reg_term_next) != len(self.reg_terms_last()):
            self._change_imgr_params = True
            return

        if len(self.dat_term_next) != len(self.dat_terms_last()):
            self._change_imgr_params = True
            return

        for term in sorted(self.dat_term_next.keys()):
            if term not in self.dat_terms_last().keys():
                self._change_imgr_params = True
                return

        for term in sorted(self.reg_term_next.keys()):
            if term not in self.reg_terms_last().keys():
                self._change_imgr_params = True
                return

        if ((self.prior_next.psize != self.prior_last().psize) or
            (self.prior_next.xdim != self.prior_last().xdim) or
            (self.prior_next.ydim != self.prior_last().ydim)):
            self.change_imgr_params = True
            return

    def check_limits(self):
        """Check image parameter consistency with observation.
        """

        imsize = np.max([self.prior_next.xdim, self.prior_next.ydim]) * self.prior_next.psize
        uvmax = 1.0/self.prior_next.psize
        uvmin = 1.0/imsize
        uvdists = self.obs_next.unpack('uvdist')['uvdist']
        maxbl = np.max(uvdists)
        minbl = np.max(uvdists[uvdists > 0])
        maxamp = np.max(np.abs(self.obs_next.unpack('amp')['amp']))

        if uvmax < maxbl:
            print("Warning! Pixel Spacing is larger than smallest spatial wavelength!")
        if uvmin > minbl:
            print("Warning! Field of View is smaller than largest nonzero spatial wavelength!")
        if self.flux_next > 1.2*maxamp:
            print("Warning! Specified flux is > 120% of maximum visibility amplitude!")
        if self.flux_next < .8*maxamp:
            print("Warning! Specified flux is < 80% of maximum visibility amplitude!")

    def reg_terms_last(self):
        """Return last used regularizer terms.
        """

        if self.nruns == 0:
            print("No imager runs yet!")
            return
        return self._reg_term_list[-1]

    def dat_terms_last(self):
        """Return last used data terms.
        """
        if self.nruns == 0:
            print("No imager runs yet!")
            return
        return self._dat_term_list[-1]

    def obs_last(self):
        """Return last used observation.
        """
        if self.nruns == 0:
            print("No imager runs yet!")
            return
        return self._obs_list[-1]

    def prior_last(self):
        """Return last used prior image.
        """
        if self.nruns == 0:
            print("No imager runs yet!")
            return
        return self._prior_list[-1]

    def out_last(self):
        """Return last result.
        """
        if self.nruns == 0:
            print("No imager runs yet!")
            return
        return self._out_list[-1]

    def out_scattered_last(self):
        """Return last result with scattering.
        """
        if self.nruns == 0 or len(self._out_list_scattered) == 0:
            print("No stochastic optics imager runs yet!")
            return
        return self._out_list_scattered[-1]

    def out_epsilon_last(self):
        """Return last result with scattering.
        """
        if self.nruns == 0 or len(self._out_list_epsilon) == 0:
            print("No stochastic optics imager runs yet!")
            return
        return self._out_list_epsilon[-1]

    def init_last(self):
        """Return last initial image.
        """
        if self.nruns == 0:
            print("No imager runs yet!")
            return
        return self._init_list[-1]

    def flux_last(self):
        """Return last total flux constraint.
        """
        if self.nruns == 0:
            print("No imager runs yet!")
            return
        return self._flux_list[-1]

    def clipfloor_last(self):
        """Return last clip floor.
        """
        if self.nruns == 0:
            print("No imager runs yet!")
            return
        return self._clipfloor_list[-1]

    def maxit_last(self):
        """Return last max_iterations value.
        """
        if self.nruns == 0:
            print("No imager runs yet!")
            return
        return self._maxit_list[-1]

    def transform_last(self):
        """Return last image transfrom used.
        """
        if self.nruns == 0:
            print("No imager runs yet!")
            return
        return self._transform_list[-1]

    def ttype_last(self):
        """Return last fourier transform type used.
        """
        if self.nruns == 0:
            print("No imager runs yet!")
            return
        return self._ttype_list[-1]

    def init_imager_I(self):
        """Set up Stokes I imager.
        """

        # embedding, prior & initial image vectors
        self.set_embed()
        self._nprior_I = (self.flux_next * self.prior_next.imvec / np.sum((self.prior_next.imvec)[self._embed_mask]))[self._embed_mask]
        self._ninit_I = (self.flux_next * self.init_next.imvec / np.sum((self.init_next.imvec)[self._embed_mask]))[self._embed_mask]

        # data term tuples
        if self._change_imgr_params:
            self._data_tuples = {}
            for dname in list(self.dat_term_next.keys()):
                tup = chisqdata(self.obs_next, self.prior_next, self._embed_mask, dname, 
                                ttype=self.ttype_next, order=self.fft_interp_order, fft_pad_factor=self.fft_pad_factor, 
                                conv_func=self.fft_conv_func, p_rad=self.fft_gridder_prad, debias=self.debias, 
                                snrcut=self.camp_snrcut,systematic_noise=self.systematic_noise)
                self._data_tuples[dname] = tup
            self._change_imgr_params = False

        return

    def init_imager_scattering(self):
        """Set up scattering imager.
        """
        if self.scattering_model == None:
            self.scattering_model = so.ScatteringModel()

        # First some preliminary definitions
        wavelength = C/self.obs_next.rf*100.0 #Observing wavelength [cm]
        wavelengthbar = wavelength/(2.0*np.pi) #lambda/(2pi) [cm]
        N = self.prior_next.xdim
        FOV = self.prior_next.psize * N * self.scattering_model.observer_screen_distance #Field of view, in cm, at the scattering screen

        # The ensemble-average convolution kernel and its gradients
        self._ea_ker = self.scattering_model.Ensemble_Average_Kernel(self.prior_next, wavelength_cm = wavelength)
        ea_ker_gradient = so.Wrapped_Gradient(self._ea_ker/(FOV/N))
        self._ea_ker_gradient_x = -ea_ker_gradient[1]
        self._ea_ker_gradient_y = -ea_ker_gradient[0]

        # The power spectrum (note: rotation is not currently implemented; the gradients would need to be modified slightly)
        self._sqrtQ = np.real(self.scattering_model.sqrtQ_Matrix(self.prior_next,t_hr=0.0))


    def make_chisq_dict(self, imvec):
        """make dictionary of current chi^2 term values
        """
        chi2_dict = {}
        for dname in sorted(self.dat_term_next.keys()):
            data = self._data_tuples[dname][0]
            sigma = self._data_tuples[dname][1]
            A = self._data_tuples[dname][2]

            chi2 = chisq(imvec, A, data, sigma, dname, ttype=self.ttype_next, mask=self._embed_mask)
            chi2_dict[dname] = chi2

        return chi2_dict

    def make_chisqgrad_dict(self, imvec):
        """make dictionary of current chi^2 term gradient values
        """
        chi2grad_dict = {}
        for dname in sorted(self.dat_term_next.keys()):
            data = self._data_tuples[dname][0]
            sigma = self._data_tuples[dname][1]
            A = self._data_tuples[dname][2]

            chi2grad = chisqgrad(imvec, A, data, sigma, dname, ttype=self.ttype_next, mask=self._embed_mask)
            chi2grad_dict[dname] = chi2grad

        return chi2grad_dict

    def make_reg_dict(self, imvec):
        """make dictionary of current regularizer values
        """
        reg_dict = {}
        for regname in sorted(self.reg_term_next.keys()):
            # incorporate flux and cm into the generic "regularizer"  function!
            if regname == 'flux':
                #norm = flux**2
                norm = 1
                reg = (np.sum(imvec) - self.flux_next)**2
                reg /= norm

            elif regname == 'cm':
                #norm = flux**2 * self.prior_next.psize**2
                norm = 1
                reg = (np.sum(imvec*self._coord_matrix[:,0])**2 +
                       np.sum(imvec*self._coord_matrix[:,1])**2)
                reg /= norm

            else:
                reg = regularizer(imvec, self._nprior_I, self._embed_mask,
                                  self.flux_next, self.prior_next.xdim,
                                  self.prior_next.ydim, self.prior_next.psize,
                                  regname)
            reg_dict[regname] = reg

        return reg_dict

    def make_reggrad_dict(self, imvec):
        """make dictionary of current regularizer gradient values
        """
        reggrad_dict = {}
        for regname in sorted(self.reg_term_next.keys()):
            # incorporate flux and cm into the generic "regularizer"  function!
            if regname == 'flux':
                #norm = flux**2
                norm = 1
                reg = 2*(np.sum(imvec) - self.flux_next)
                reg /= norm

            elif regname == 'cm':
                #norm = flux**2 * self.prior_next.psize**2
                norm = 1
                reg = 2*(np.sum(imvec*self._coord_matrix[:,0])*self._coord_matrix[:,0] +
                         np.sum(imvec*self._coord_matrix[:,1])*self._coord_matrix[:,1])
                reg /= norm

            else:
                reg = regularizergrad(imvec, self._nprior_I, self._embed_mask,
                                      self.flux_next, self.prior_next.xdim,
                                      self.prior_next.ydim, self.prior_next.psize,
                                      regname)
            reggrad_dict[regname] = reg

        return reggrad_dict

    def objfunc(self, imvec):
        """Current objective function.
        """
        if self.transform_next == 'log':
            imvec = np.exp(imvec)

        datterm = 0.
        chi2_term_dict = self.make_chisq_dict(imvec)
        for dname in sorted(self.dat_term_next.keys()):
            datterm += self.dat_term_next[dname] * (chi2_term_dict[dname] - 1.)

        regterm = 0
        reg_term_dict = self.make_reg_dict(imvec)
        for regname in sorted(self.reg_term_next.keys()):
            regterm += self.reg_term_next[regname] * reg_term_dict[regname]

        return datterm + regterm

    def objfunc_scattering(self, minvec):
        """Current stochastic optics objective function.
        """
        N = self.prior_next.xdim

        imvec       = minvec[:N**2]
        EpsilonList = minvec[N**2:]
        if self.transform_next == 'log':
            imvec = np.exp(imvec)

        IM = ehtim.image.Image(imvec.reshape(N,N), self.prior_next.psize, self.prior_next.ra, 
                               self.prior_next.dec, rf=self.obs_next.rf, 
                               source=self.prior_next.source, mjd=self.prior_next.mjd)

        #the scattered image vector
        scatt_im = self.scattering_model.Scatter(IM, Epsilon_Screen=so.MakeEpsilonScreenFromList(EpsilonList, N), 
                                                 ea_ker = self._ea_ker, sqrtQ=self._sqrtQ, 
                                                 Linearized_Approximation=True).imvec 

        # Calculate the chi^2 using the scattered image
        datterm = 0.
        chi2_term_dict = self.make_chisq_dict(scatt_im)
        for dname in sorted(self.dat_term_next.keys()):
            datterm += self.dat_term_next[dname] * (chi2_term_dict[dname] - 1.)

        # Calculate the entropy using the unscattered image
        regterm = 0
        reg_term_dict = self.make_reg_dict(imvec)
        for regname in sorted(self.reg_term_next.keys()):
            regterm += self.reg_term_next[regname] * reg_term_dict[regname]

        # Scattering screen regularization term
        chisq_epsilon = sum(EpsilonList*EpsilonList)/((N*N-1.0)/2.0)
        regterm_scattering = self.alpha_phi_next * (chisq_epsilon - 1.0)

        return datterm + regterm + regterm_scattering

    def objgrad(self, imvec):
        """Current objective function gradient.
        """
        if self.transform_next == 'log':
            imvec = np.exp(imvec)

        datterm = 0.
        chi2_term_dict = self.make_chisqgrad_dict(imvec)
        for dname in sorted(self.dat_term_next.keys()):
            datterm += self.dat_term_next[dname] * (chi2_term_dict[dname] - 1.)

        regterm = 0
        reg_term_dict = self.make_reggrad_dict(imvec)
        for regname in sorted(self.reg_term_next.keys()):
            regterm += self.reg_term_next[regname] * reg_term_dict[regname]

        grad = datterm + regterm

        # chain rule term for change of variables
        if self.transform_next == 'log':
            grad *= imvec

        return grad

    def objgrad_scattering(self, minvec):
        """Current stochastic optics objective function gradient
        """
        wavelength = C/self.obs_next.rf*100.0 #Observing wavelength [cm]
        wavelengthbar = wavelength/(2.0*np.pi) #lambda/(2pi) [cm]
        N = self.prior_next.xdim
        #Field of view, in cm, at the scattering screen
        FOV = self.prior_next.psize * N * self.scattering_model.observer_screen_distance 
        rF = self.scattering_model.rF(wavelength)

        imvec       = minvec[:N**2]
        EpsilonList = minvec[N**2:]
        if self.transform_next == 'log':
            imvec = np.exp(imvec)

        IM = ehtim.image.Image(imvec.reshape(N,N), self.prior_next.psize, self.prior_next.ra, 
                               self.prior_next.dec, rf=self.obs_next.rf, source=self.prior_next.source, 
                               mjd=self.prior_next.mjd)
        #the scattered image vector
        scatt_im = self.scattering_model.Scatter(IM, Epsilon_Screen=so.MakeEpsilonScreenFromList(EpsilonList, N),
                                                 ea_ker = self._ea_ker, sqrtQ=self._sqrtQ, 
                                                 Linearized_Approximation=True).imvec 

        EA_Image = self.scattering_model.Ensemble_Average_Blur(IM, ker = self._ea_ker)
        EA_Gradient = so.Wrapped_Gradient((EA_Image.imvec/(FOV/N)).reshape(N, N))
        #The gradient signs don't actually matter, but let's make them match intuition (i.e., right to left, bottom to top)
        EA_Gradient_x = -EA_Gradient[1]
        EA_Gradient_y = -EA_Gradient[0]

        Epsilon_Screen = so.MakeEpsilonScreenFromList(EpsilonList, N)
        phi = self.scattering_model.MakePhaseScreen(Epsilon_Screen, IM, obs_frequency_Hz=self.obs_next.rf,sqrtQ_init=self._sqrtQ).imvec.reshape((N, N))
        phi_Gradient = so.Wrapped_Gradient(phi/(FOV/N))
        phi_Gradient_x = -phi_Gradient[1]
        phi_Gradient_y = -phi_Gradient[0]

        #Entropy gradient; wrt unscattered image so unchanged by scattering
        regterm = 0
        reg_term_dict = self.make_reggrad_dict(imvec)
        for regname in sorted(self.reg_term_next.keys()):
            regterm += self.reg_term_next[regname] * reg_term_dict[regname]

        # Chi^2 gradient wrt the unscattered image
        # First, the chi^2 gradient wrt to the scattered image
        datterm = 0.
        chi2_term_dict = self.make_chisqgrad_dict(scatt_im)
        for dname in sorted(self.dat_term_next.keys()):
            datterm += self.dat_term_next[dname] * (chi2_term_dict[dname] - 1.)
        dchisq_dIa = datterm.reshape((N,N))
        # Now the chain rule factor to get the chi^2 gradient wrt the unscattered image
        gx = (rF**2.0 * so.Wrapped_Convolve(self._ea_ker_gradient_x[::-1,::-1], phi_Gradient_x * (dchisq_dIa))).flatten()
        gy = (rF**2.0 * so.Wrapped_Convolve(self._ea_ker_gradient_y[::-1,::-1], phi_Gradient_y * (dchisq_dIa))).flatten()
        chisq_grad_im = so.Wrapped_Convolve(self._ea_ker[::-1,::-1], (dchisq_dIa)).flatten() + gx + gy

        # Gradient of the data chi^2 wrt to the epsilon screen
        #Preliminary Definitions
        chisq_grad_epsilon = np.zeros(N**2-1)
        i_grad = 0
        ell_mat = np.zeros((N,N))
        m_mat   = np.zeros((N,N))
        for ell in range(0, N):
            for m in range(0, N):
                ell_mat[ell,m] = ell
                m_mat[ell,m] = m

        #Real part; top row
        for t in range(1, (N+1)/2):
            s=0
            grad_term = so.Wrapped_Gradient(wavelengthbar/FOV*self._sqrtQ[s][t]*2.0*np.cos(2.0*np.pi/N*(ell_mat*s + m_mat*t))/(FOV/N))
            grad_term_x = -grad_term[1]
            grad_term_y = -grad_term[0]
            chisq_grad_epsilon[i_grad] = np.sum( dchisq_dIa * rF**2 * ( EA_Gradient_x * grad_term_x + EA_Gradient_y * grad_term_y ) )
            i_grad = i_grad + 1

        #Real part; remainder
        for s in range(1,(N+1)/2):
            for t in range(N):
                grad_term = so.Wrapped_Gradient(wavelengthbar/FOV*self._sqrtQ[s][t]*2.0*np.cos(2.0*np.pi/N*(ell_mat*s + m_mat*t))/(FOV/N))
                grad_term_x = -grad_term[1]
                grad_term_y = -grad_term[0]
                chisq_grad_epsilon[i_grad] = np.sum( dchisq_dIa * rF**2 * ( EA_Gradient_x * grad_term_x + EA_Gradient_y * grad_term_y ) )
                i_grad = i_grad + 1

        #Imaginary part; top row
        for t in range(1, (N+1)/2):
            s=0
            grad_term = so.Wrapped_Gradient(-wavelengthbar/FOV*self._sqrtQ[s][t]*2.0*np.sin(2.0*np.pi/N*(ell_mat*s + m_mat*t))/(FOV/N))
            grad_term_x = -grad_term[1]
            grad_term_y = -grad_term[0]
            chisq_grad_epsilon[i_grad] = np.sum( dchisq_dIa * rF**2 * ( EA_Gradient_x * grad_term_x + EA_Gradient_y * grad_term_y ) )
            i_grad = i_grad + 1

        #Imaginary part; remainder
        for s in range(1,(N+1)/2):
            for t in range(N):
                grad_term = so.Wrapped_Gradient(-wavelengthbar/FOV*self._sqrtQ[s][t]*2.0*np.sin(2.0*np.pi/N*(ell_mat*s + m_mat*t))/(FOV/N))
                grad_term_x = -grad_term[1]
                grad_term_y = -grad_term[0]
                chisq_grad_epsilon[i_grad] = np.sum( dchisq_dIa * rF**2 * ( EA_Gradient_x * grad_term_x + EA_Gradient_y * grad_term_y ) )
                i_grad = i_grad + 1

        # Gradient of the chi^2 regularization term for the epsilon screen
        chisq_epsilon_grad = self.alpha_phi_next * 2.0*EpsilonList/((N*N-1)/2.0)

        # chain rule term for change of variables
        if self.transform_next == 'log':
            regterm       *= imvec
            chisq_grad_im *= imvec

        return np.concatenate(((regterm + chisq_grad_im),(chisq_grad_epsilon + chisq_epsilon_grad)))

    def plotcur(self, imvec):
        if self._show_updates:
            if self.transform_next == 'log':
                imvec = np.exp(imvec)
            chi2_term_dict = self.make_chisq_dict(imvec)
            reg_term_dict = self.make_reg_dict(imvec)

            chi2_keys = sorted(chi2_term_dict.keys())
            chi2_1 = chi2_term_dict[chi2_keys[0]]
            chi2_2 = 0.
            if len(chi2_term_dict) > 1:
                chi2_2 = chi2_term_dict[chi2_keys[1]]

            outstr = "i: %d " % self._nit

            for dname in sorted(self.dat_term_next.keys()):
                outstr += "%s : %0.2f " % (dname, chi2_term_dict[dname]*self.dat_term_next[dname])
            for regname in sorted(self.reg_term_next.keys()):
                outstr += "%s : %0.2f " % (regname, reg_term_dict[regname]*self.reg_term_next[regname])

            if np.any(np.invert(self._embed_mask)): imvec = embed(imvec, self._embed_mask)
            plot_i(imvec, self.prior_next, self._nit, chi2_1, chi2_2, ipynb=False)

            print(outstr)
        self._nit += 1

    def plotcur_scattering(self, minvec):
        if self._show_updates:
            N = self.prior_next.xdim

            imvec       = minvec[:N**2]
            EpsilonList = minvec[N**2:]
            if self.transform_next == 'log':
                imvec = np.exp(imvec)

            IM = ehtim.image.Image(imvec.reshape(N,N), self.prior_next.psize, self.prior_next.ra, 
                                   self.prior_next.dec, rf=self.obs_next.rf, 
                                   source=self.prior_next.source, mjd=self.prior_next.mjd)
            #the scattered image vector
            scatt_im = self.scattering_model.Scatter(IM, Epsilon_Screen=so.MakeEpsilonScreenFromList(EpsilonList, N),
                                                     ea_ker = self._ea_ker, sqrtQ=self._sqrtQ, Linearized_Approximation=True).imvec 

            # Calculate the chi^2 using the scattered image
            datterm = 0.
            chi2_term_dict = self.make_chisq_dict(scatt_im)
            for dname in sorted(self.dat_term_next.keys()):
                datterm += self.dat_term_next[dname] * (chi2_term_dict[dname] - 1.)

            # Calculate the entropy using the unscattered image
            regterm = 0
            reg_term_dict = self.make_reg_dict(imvec)
            for regname in sorted(self.reg_term_next.keys()):
                regterm += self.reg_term_next[regname] * reg_term_dict[regname]

            # Scattering screen regularization term
            chisq_epsilon = sum(EpsilonList*EpsilonList)/((N*N-1.0)/2.0)
            regterm_scattering = self.alpha_phi_next * (chisq_epsilon - 1.0)

            outstr = "i: %d " % self._nit

            for dname in sorted(self.dat_term_next.keys()):
                outstr += "%s : %0.2f " % (dname, chi2_term_dict[dname])
            for regname in sorted(self.reg_term_next.keys()):
                outstr += "%s : %0.2f " % (regname, reg_term_dict[regname])
            outstr += "Epsilon chi^2 : %0.2f " % (chisq_epsilon)
            outstr += "Max |Epsilon| : %0.2f " % (max(abs(EpsilonList)))
            print(outstr)

        self._nit += 1

    def make_image_I(self, grads=True, show_updates=True):
        """Make Stokes I image using current imager settings.
        """
        # Checks and initialize
        self.check_params()
        self.check_limits()
        self.init_imager_I()

        # Generate and the initial image
        if self.transform_next == 'log': xinit = np.log(self._ninit_I)
        else: xinit = self._ninit_I
        self._nit = 0

        # Print stats
        if show_updates: self._show_updates=True
        else: self._show_updates=False
        self.plotcur(xinit)

        # Minimize
        optdict = {'maxiter':self.maxit_next, 'ftol':STOP, 'maxcor':NHIST}
        tstart = time.time()
        if grads:
            res = opt.minimize(self.objfunc, xinit, method='L-BFGS-B', jac=self.objgrad,
                               options=optdict, callback=self.plotcur)
        else:
            res = opt.minimize(self.objfunc, xinit, method='L-BFGS-B',
                               options=optdict, callback=self.plotcur)
        tstop = time.time()

        # Format output
        out = res.x
        if self.transform_next == 'log': out = np.exp(res.x)
        if np.any(np.invert(self._embed_mask)): out = embed(out, self._embed_mask)

        outim = image.Image(out.reshape(self.prior_next.ydim, self.prior_next.xdim),
                            self.prior_next.psize, self.prior_next.ra, self.prior_next.dec,
                            rf=self.prior_next.rf, source=self.prior_next.source,
                            mjd=self.prior_next.mjd, pulse=self.prior_next.pulse)

        # Preserving image complex polarization fractions
        if len(self.prior_next.qvec):
            qvec = self.prior_next.qvec * out / self.prior_next.imvec
            uvec = self.prior_next.uvec * out / self.prior_next.imvec
            outim.add_qu(qvec.reshape(self.prior_next.ydim, self.prior_next.xdim),
                         uvec.reshape(self.prior_next.ydim, self.prior_next.xdim))

        # Print stats
        print("time: %f s" % (tstop - tstart))
        print("J: %f" % res.fun)
        print(res.message)

        # Append to history
        logstr = str(self.nruns) + ": make_image_I()" #TODO - what should the log string be?
        self._append_image_history(outim, logstr)
        self.nruns += 1

        # Return Image object
        return outim

    def make_image_I_stochastic_optics(self, grads=True, show_updates=True):
        """Reconstructs an image of total flux density using the stochastic optics scattering mitigation technique.
           Uses the scattering model of the imager. If none has been specified, it will default to a standard model for Sgr A*.
           Returns the estimated unscattered image.

           Args:
                grads (bool): Flag for whether or not to use analytic gradients.
                show_updates (bool): Flag for whether or not to show updates for each step of convergence.
           Returns:
               out (Image): The estimated *unscattered* image.
        """

        N = self.prior_next.xdim

        # Checks and initialize
        self.check_params()
        self.check_limits()
        self.init_imager_I()
        self.init_imager_scattering()

        # Generate the initial image+screen vector. By default, the screen is re-initialized to zero each time.
        if self.transform_next == 'log':
            xinit = np.log(self._ninit_I)
        else:
            xinit = self._ninit_I

        if len(self.epsilon_list_next) == 0:
            xinit = np.concatenate((xinit,np.zeros(N**2-1)))
        else:
            xinit = np.concatenate((xinit,self.epsilon_list_next))
        

        self._nit = 0

        # Print stats
        if show_updates:
            self._show_updates=True
        else:
            self._show_updates=False

        self.plotcur_scattering(xinit)

        # Minimize
        optdict = {'maxiter':self.maxit_next, 'ftol':STOP, 'maxcor':NHIST}
        tstart = time.time()
        if grads:
            res = opt.minimize(self.objfunc_scattering, xinit, method='L-BFGS-B', jac=self.objgrad_scattering,
                               options=optdict, callback=self.plotcur_scattering)
        else:
            res = opt.minimize(self.objfunc_scattering, xinit, method='L-BFGS-B',
                               options=optdict, callback=self.plotcur_scattering)
        tstop = time.time()

        # Format output
        out = res.x[:N**2]
        if self.transform_next == 'log': out = np.exp(out)
        if np.any(np.invert(self._embed_mask)):
            raise Exception("Embedding is not currently implemented!")
            out = embed(out, self._embed_mask)

        outim = image.Image(out.reshape(N, N),
                            self.prior_next.psize, self.prior_next.ra, self.prior_next.dec,
                            rf=self.prior_next.rf, source=self.prior_next.source,
                            mjd=self.prior_next.mjd, pulse=self.prior_next.pulse)
        outep = res.x[N**2:]
        outscatt = self.scattering_model.Scatter(outim, Epsilon_Screen=so.MakeEpsilonScreenFromList(outep, N), 
                                                 ea_ker = self._ea_ker, sqrtQ=self._sqrtQ, 
                                                 Linearized_Approximation=True)

        # Preserving image complex polarization fractions
        if len(self.prior_next.qvec):
            qvec = self.prior_next.qvec * out / self.prior_next.imvec
            uvec = self.prior_next.uvec * out / self.prior_next.imvec
            outim.add_qu(qvec.reshape(N, N),
                         uvec.reshape(N, N))

        # Print stats
        print("time: %f s" % (tstop - tstart))
        print("J: %f" % res.fun)
        print(res.message)

        # Append to history
        logstr = str(self.nruns) + ": make_image_I_stochastic_optics()"
        self._append_image_history(outim, logstr)
        self._out_list_epsilon.append(res.x[N**2:])
        self._out_list_scattered.append(outscatt)

        self.nruns += 1

        # Return Image object
        return outim

    def _append_image_history(self, outim, logstr):
        self.logstr += (logstr + "\n")
        self._obs_list.append(self.obs_next)
        self._init_list.append(self.init_next)
        self._prior_list.append(self.prior_next)
        self._flux_list.append(self.flux_next)
        self._clipfloor_list.append(self.clipfloor_next)
        self._maxit_list.append(self.maxit_next)
        self._transform_list.append(self.transform_next)
        self._reg_term_list.append(self.reg_term_next)
        self._ttype_list.append(self.ttype_next)
        self._dat_term_list.append(self.dat_term_next)
        self._alpha_phi_list.append(self.alpha_phi_next)

        self._out_list.append(outim)
        return

    def make_image_P(self):
        return

    def make_image_scat(self):
        return






