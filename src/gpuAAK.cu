#include "stdio.h"

#include "global.h"
#include "Utility.hh"

#ifdef __CUDACC__
#else
#include <gsl/gsl_sf_bessel.h>
#endif

#define  NUM_THREADS 32


CUDA_CALLABLE_MEMBER
double d_dot_product(const double *u,const double *v){
    return u[0]*v[0] + u[1]*v[1] + u[2]*v[2];
}

CUDA_CALLABLE_MEMBER
void d_cross(const double *u,const double *v,double *w){
  w[0] = u[1]*v[2]-u[2]*v[1];
  w[1] = u[2]*v[0]-u[0]*v[2];
  w[2] = u[0]*v[1]-u[1]*v[0];
}

CUDA_CALLABLE_MEMBER
double d_vec_norm(const double *u){
    return sqrt(u[0]*u[0] + u[1]*u[1] + u[2]*u[2]);
}

CUDA_CALLABLE_MEMBER
void d_RotCoeff(double* rot, double* n, double* L, double* S, double* nxL, double* nxS,
                double iota,double theta_S,double phi_S,double theta_K,double phi_K,double alpha){

  n[0] = sin(theta_S)*cos(phi_S);
  n[1] = sin(theta_S)*sin(phi_S);
  n[2] = cos(theta_S);
  S[0] = sin(theta_K)*cos(phi_K);
  S[1] = sin(theta_K)*sin(phi_K);
  S[2] = cos(theta_K);
  L[0] = cos(iota)*sin(theta_K)*cos(phi_K)+sin(iota)*(sin(alpha)*sin(phi_K)-cos(alpha)*cos(theta_K)*cos(phi_K));
  L[1] = cos(iota)*sin(theta_K)*sin(phi_K)-sin(iota)*(sin(alpha)*cos(phi_K)+cos(alpha)*cos(theta_K)*sin(phi_K));
  L[2] = cos(iota)*cos(theta_K)+sin(iota)*cos(alpha)*sin(theta_K);
  d_cross(n,L,nxL);
  d_cross(n,S,nxS);

  double norm=d_vec_norm(nxL)*d_vec_norm(nxS);
  double dot,cosrot,sinrot;
  //gsl_blas_ddot(nxL,nxS,&dot);
  dot = d_dot_product(nxL,nxS);

  if (norm < 1e-6) norm = 1e-6;

  cosrot=dot/norm;
  //gsl_blas_ddot(L,nxS,&dot);
  dot = d_dot_product(L,nxS);
  sinrot=dot;
  //gsl_blas_ddot(S,nxL,&dot);
  dot = d_dot_product(S,nxL);
  sinrot-=dot;
  sinrot/=norm;

  rot[0]=2.*cosrot*cosrot-1.;
  rot[1]=cosrot*sinrot;
  rot[2]=-rot[1];
  rot[3]=rot[0];
}

#define  NUM_PARS 8
#define MAX_SPLINE_POINTS 160
CUDA_KERNEL
void make_waveform(cmplx *waveform,
              double* interp_array,
              double M_phys, double S_phys, double mu, double qS, double phiS, double qK, double phiK, double dist,
              int nmodes, bool mich,
              double delta_t, double *start_t_all, int *interval_inds, int data_length, int init_length)
{

      cmplx I(0.0, 1.0);

      #ifdef __CUDACC__

      __shared__ double rot_all[4 * NUM_THREADS];
      __shared__ double n_all[3 * NUM_THREADS];
      __shared__ double L_all[3 * NUM_THREADS];
      __shared__ double S_all[3 * NUM_THREADS];
      __shared__ double nxL_all[3 * NUM_THREADS];
      __shared__ double nxS_all[3 * NUM_THREADS];

      double* rot = &rot_all[threadIdx.x * 4];
      double* n_rot = &n_all[threadIdx.x * 3];
      double* L_rot = &L_all[threadIdx.x * 3];
      double* S_rot = &S_all[threadIdx.x * 3];
      double* nxL_rot = &nxL_all[threadIdx.x * 3];
      double* nxS_rot = &nxS_all[threadIdx.x * 3];

      #endif

      CUDA_SHARED double spline_coeffs[NUM_PARS * 4 * MAX_SPLINE_POINTS];
      CUDA_SHARED double start_t[MAX_SPLINE_POINTS];

      int start, end, increment;
      #ifdef __CUDACC__
      start = threadIdx.x;
      end = 4 * NUM_PARS * init_length;
      increment = blockDim.x;
      #else
      start = 0;
      end = 4 * NUM_PARS * init_length;
      increment = 1;

      #ifdef __USE_OMP__
      #pragma omp parallel for
      #endif  // __USE_OMP__
      #endif // __CUDACC__

       // prepare interpolants
      // 8 parameters, 4 coefficient values for each parameter
      for (int i = start; i < end; i += increment)
      {
          spline_coeffs[i] = interp_array[i];
      }

      CUDA_SYNC_THREADS;

      for (int i = start; i < init_length; i += increment)
      {
          start_t[i] = start_t_all[i];
      }

      // unroll coefficients
      double fill_val = 1e-6;
      if (qS < fill_val) qS = fill_val;
      if (qK < fill_val) qK = fill_val;
      if (qS > M_PI - fill_val) qS = M_PI - fill_val;
      if (qK > M_PI - fill_val) qK = M_PI - fill_val;

      double cosqS=cos(qS);
      double sinqS=sin(qS);
      double cosqK=cos(qK);
      double sinqK=sin(qK);
      double cosphiK=cos(phiK);
      double sinphiK=sin(phiK);
      double halfsqrt3=sqrt(3.)/2.;
      double mu_sec = mu * MTSUN_SI;
      double zeta=mu_sec/dist/GPCINSEC; // M/D

      #ifdef __CUDACC__

      start = 0 + threadIdx.x + blockIdx.x * blockDim.x;
      end = data_length;
      increment = blockDim.x * gridDim.x;

      #else

      start = 0;
      end = data_length;
      increment = 1;

      #ifdef __USE_OMP__
      #pragma omp parallel for
      #endif
      #endif
      for (int i = start; i < end; i += increment)
      {

          #ifdef __CUDACC__
          #else

          double rot_temp[4];
          double n_temp[3];
          double L_temp[3];
          double S_temp[3];
          double nxL_temp[3];
          double nxS_temp[3];

          double* rot = &rot_temp[0];
          double* n_rot = &n_temp[0];
          double* L_rot = &L_temp[0];
          double* S_rot = &S_temp[0];
          double* nxL_rot = &nxL_temp[0];
          double* nxS_rot = &nxS_temp[0];

          #endif
          waveform[i] = cmplx(0.0, 0.0);

          double t=delta_t * i;

          int old_ind = interval_inds[i];
          double start_t_i = start_t[old_ind];

          // p_y = spline_coeffs[(0 * init_length + old_ind) * NUM_PARS + 0]; p_c1 = spline_coeffs[(1 * init_length + old_ind) * NUM_PARS + 0]; p_c2 = spline_coeffs[(2 * init_length + old_ind) * NUM_PARS + 0]; p_c3 = spline_coeffs[(3 * init_length + old_ind) * NUM_PARS + 0];
          //int index = (coeff_num * init_length + old_ind) * NUM_PARS + par_num;
          double e_y = spline_coeffs[(0 * init_length + old_ind) * NUM_PARS + 0];
          double e_c1 = spline_coeffs[(1 * init_length + old_ind) * NUM_PARS + 0];
          double e_c2 = spline_coeffs[(2 * init_length + old_ind) * NUM_PARS + 0];
          double e_c3 = spline_coeffs[(3 * init_length + old_ind) * NUM_PARS + 0];

          double Phi_y = spline_coeffs[(0 * init_length + old_ind) * NUM_PARS + 1];
          double Phi_c1 = spline_coeffs[(1 * init_length + old_ind) * NUM_PARS + 1];
          double Phi_c2 = spline_coeffs[(2 * init_length + old_ind) * NUM_PARS + 1];
          double Phi_c3 = spline_coeffs[(3 * init_length + old_ind) * NUM_PARS + 1];
          double gim_y = spline_coeffs[(0 * init_length + old_ind) * NUM_PARS + 2];
          double gim_c1 = spline_coeffs[(1 * init_length + old_ind) * NUM_PARS + 2];
          double gim_c2 = spline_coeffs[(2 * init_length + old_ind) * NUM_PARS + 2];
          double gim_c3 = spline_coeffs[(3 * init_length + old_ind) * NUM_PARS + 2];
          double alp_y = spline_coeffs[(0 * init_length + old_ind) * NUM_PARS + 3];
          double alp_c1 = spline_coeffs[(1 * init_length + old_ind) * NUM_PARS + 3];
          double alp_c2 = spline_coeffs[(2 * init_length + old_ind) * NUM_PARS + 3];
          double alp_c3 = spline_coeffs[(3 * init_length + old_ind) * NUM_PARS + 3];

          double nu_y = spline_coeffs[(0 * init_length + old_ind) * NUM_PARS + 4];
          double nu_c1 = spline_coeffs[(1 * init_length + old_ind) * NUM_PARS + 4];
          double nu_c2 = spline_coeffs[(2 * init_length + old_ind) * NUM_PARS + 4];
          double nu_c3 = spline_coeffs[(3 * init_length + old_ind) * NUM_PARS + 4];

          double gimdot_y = spline_coeffs[(0 * init_length + old_ind) * NUM_PARS + 5];
          double gimdot_c1 = spline_coeffs[(1 * init_length + old_ind) * NUM_PARS + 5];
          double gimdot_c2 = spline_coeffs[(2 * init_length + old_ind) * NUM_PARS + 5];
          double gimdot_c3 = spline_coeffs[(3 * init_length + old_ind) * NUM_PARS + 5];
          double OmegaPhi_y = spline_coeffs[(0 * init_length + old_ind) * NUM_PARS + 6];
          double OmegaPhi_c1 = spline_coeffs[(1 * init_length + old_ind) * NUM_PARS + 6];
          double OmegaPhi_c2 = spline_coeffs[(2 * init_length + old_ind) * NUM_PARS + 6];
          double OmegaPhi_c3 = spline_coeffs[(3 * init_length + old_ind) * NUM_PARS + 6];
          double lam_y = spline_coeffs[(0 * init_length + old_ind) * NUM_PARS + 7];
          double lam_c1 = spline_coeffs[(1 * init_length + old_ind) * NUM_PARS + 7];
          double lam_c2 = spline_coeffs[(2 * init_length + old_ind) * NUM_PARS + 7];
          double lam_c3 = spline_coeffs[(3 * init_length + old_ind) * NUM_PARS + 7];

          double x = t - start_t_i;
          double x2 = x * x;
          double x3 = x * x2;

          // double v = p_y + p_c1 * x + p_c2 * x2 + p_c3 * x3;
          double e = e_y + e_c1 * x + e_c2 * x2 + e_c3 * x3;
          double Phi = Phi_y + Phi_c1 * x + Phi_c2 * x2 + Phi_c3 * x3;
          double gim = gim_y + gim_c1 * x + gim_c2 * x2 + gim_c3 * x3;
          double alp = alp_y + alp_c1 * x + alp_c2 * x2 + alp_c3 * x3;
          double nu = nu_y + nu_c1 * x + nu_c2 * x2 + nu_c3 * x3;
          double gimdot = gimdot_y + gimdot_c1 * x + gimdot_c2 * x2 + gimdot_c3 * x3;
          double OmegaPhi = OmegaPhi_y + OmegaPhi_c1 * x + OmegaPhi_c2 * x2 + OmegaPhi_c3 * x3;
          double lam = lam_y + lam_c1 * x + lam_c2 * x2 + lam_c3 * x3;

          if (lam > M_PI - fill_val) lam = M_PI - fill_val;
          if (lam < fill_val) lam = fill_val;

          double coslam=cos(lam);
          double sinlam=sin(lam);
          double cosalp=cos(alp);
          double sinalp=sin(alp);
          double cosqL=cosqK*coslam+sinqK*sinlam*cosalp;
          double sinqL=sqrt(1.-cosqL*cosqL);
          double phiLup=sinqK*sinphiK*coslam-cosphiK*sinlam*sinalp-cosqK*sinphiK*sinlam*cosalp;
          double phiLdown=sinqK*cosphiK*coslam+sinphiK*sinlam*sinalp-cosqK*cosphiK*sinlam*cosalp;
          double phiL=atan2(phiLup,phiLdown);
          double Ldotn=cosqL*cosqS+sinqL*sinqS*cos(phiL-phiS);
          double Ldotn2=Ldotn*Ldotn;
          double Sdotn=cosqK*cosqS+sinqK*sinqS*cos(phiK-phiS);
          double beta;
          if (S_phys == 0.0)
          {
              beta = 0.0;
          }
          else
          {
              double betaup=-Sdotn+coslam*Ldotn;
              double betadown=sinqS*sin(phiK-phiS)*sinlam*cosalp+(cosqK*Sdotn-cosqS)/sinqK*sinlam*sinalp;
              beta=atan2(betaup,betadown);
          }
          double gam=2.*(gim+beta);
          double cos2gam=cos(gam);
          double sin2gam=sin(gam);

          double orbphs,cosorbphs,sinorbphs,FplusI,FcrosI,FplusII,FcrosII;
        if(mich){
          orbphs=2.*M_PI*t/YRSID_SI;
          cosorbphs=cos(orbphs-phiS);
          sinorbphs=sin(orbphs-phiS);
          double cosq=.5*cosqS-halfsqrt3*sinqS*cosorbphs;
          double phiw=orbphs+atan2(halfsqrt3*cosqS+.5*sinqS*cosorbphs,sinqS*sinorbphs);
          double psiup=.5*cosqK-halfsqrt3*sinqK*cos(orbphs-phiK)-cosq*(cosqK*cosqS+sinqK*sinqS*cos(phiK-phiS));
          double psidown=.5*sinqK*sinqS*sin(phiK-phiS)-halfsqrt3*cos(orbphs)*(cosqK*sinqS*sin(phiS)-cosqS*sinqK*sin(phiK))-halfsqrt3*sin(orbphs)*(cosqS*sinqK*cos(phiK)-cosqK*sinqS*cos(phiS));
          double psi=atan2(psiup,psidown);
          double cosq1=.5*(1.+cosq*cosq);
          double cos2phi=cos(2.*phiw);
          double sin2phi=sin(2.*phiw);
          double cos2psi=cos(2.*psi);
          double sin2psi=sin(2.*psi);
          FplusI=cosq1*cos2phi*cos2psi-cosq*sin2phi*sin2psi;
          FcrosI=cosq1*cos2phi*sin2psi+cosq*sin2phi*cos2psi;
          FplusII=cosq1*sin2phi*cos2psi+cosq*cos2phi*sin2psi;
          FcrosII=cosq1*sin2phi*sin2psi-cosq*cos2phi*cos2psi;
        }
        else
        {
            /*
            double up_ldc = (cosqS*sinqK*cos(phiS-phiK) - cosqK*sinqS);
              double dw_ldc = (sinqK*sin(phiS-phiK));
              double psi_ldc;
              if (dw_ldc != 0.0) {
                psi_ldc = atan2(up_ldc, dw_ldc);
              }
              else {
            psi_ldc = 0.5*M_PI;
              }
              double c2psi_ldc=cos(2.*psi_ldc);
              double s2psi_ldc=sin(2.*psi_ldc);

            FplusI=c2psi_ldc;
            FcrosI=-s2psi_ldc;
            FplusII=s2psi_ldc;
            FcrosII=c2psi_ldc;*/

            FplusI = 1.0;
            FcrosI = 0.0;
            FplusII = 0.0;
            FcrosII = 1.0;
        }

          double Amp=pow(abs(OmegaPhi)*M_phys*MTSUN_SI,2./3.)*zeta;

          d_RotCoeff(rot, n_rot, L_rot, S_rot, nxL_rot, nxS_rot,
                   lam,qS,phiS,qK,phiK,alp);

          double hItemp = 0.0;
          double hIItemp = 0.0;
          for(int n=1;n<=nmodes;n++)
          {

              double fn,Doppler,nPhi;
              if(mich){
                fn=n*nu+gimdot/M_PI;
                Doppler=2.*M_PI*fn*AUsec*sinqS*cosorbphs;
                nPhi=n*Phi+Doppler;
              }
              else nPhi=n*Phi;

               double ne=n*e;
               double J0, J1, J2, J3, J4;
              #ifdef __CUDACC__

              if(n==1){ J0=-1.0*j1(ne); }
              else { J0 = jn(n-2, ne); }

              J1=jn(n-1, ne);
              J2=jn(n, ne);
              J3=jn(n+1,ne);
              J4=jn(n+2,ne);

              #else

              if(n==1){ J0=-1.0*gsl_sf_bessel_J1(ne); }
              else { J0 = gsl_sf_bessel_Jn(n-2, ne); }

              J1=gsl_sf_bessel_Jn(n-1, ne);
              J2=gsl_sf_bessel_Jn(n, ne);
              J3=gsl_sf_bessel_Jn(n+1,ne);
              J4=gsl_sf_bessel_Jn(n+2,ne);

              #endif

      double a=-n*Amp*(J0-2.*e*J1+2./n*J2+2.*e*J3-J4)*cos(nPhi);
      double b=-n*Amp*sqrt(1-e*e)*(J0-2.*J2+J4)*sin(nPhi);
      double c=2.*Amp*J2*cos(nPhi);
      double Aplus=-(1.+Ldotn2)*(a*cos2gam-b*sin2gam)+c*(1-Ldotn2);
      double Acros=2.*Ldotn*(b*cos2gam+a*sin2gam);


      // ----- rotate to NK wave frame -----
      double Aplusold=Aplus;
      double Acrosold=Acros;

      Aplus=Aplusold*rot[0]+Acrosold*rot[1];
      Acros=Aplusold*rot[2]+Acrosold*rot[3];
      // ----------

      double hnI, hnII;
      if(mich){
      	hnI=halfsqrt3*(FplusI*Aplus+FcrosI*Acros);
        hnII=halfsqrt3*(FplusII*Aplus+FcrosII*Acros);
      }
      else{
      	hnI=FplusI*Aplus+FcrosI*Acros;
        hnII=FplusII*Aplus+FcrosII*Acros;
      }

      hItemp+=hnI;
      hIItemp+=hnII;
  }

    waveform[i] = cmplx(hItemp, -hIItemp);
    }
}

// with uneven spacing in t in the sparse arrays, need to determine which timesteps the dense arrays fall into
// for interpolation
// effectively the boundaries and length of each interpolation segment of the dense array in the sparse array
void find_start_inds(int start_inds[], int unit_length[], double *t_arr, double delta_t, int *length, int new_length)
{

    double T = (new_length - 1) * delta_t;
  start_inds[0] = 0;
  int i = 1;
  for (i = 1;
       i < *length;
       i += 1){

          double t = t_arr[i];

          // adjust for waveforms that hit the end of the trajectory
          if (t < T){
              start_inds[i] = (int)std::ceil(t/delta_t);
              unit_length[i-1] = start_inds[i] - start_inds[i-1];
          } else {
            start_inds[i] = new_length;
            unit_length[i-1] = new_length - start_inds[i-1];
            break;
        }

      }

  // fixes for not using certain segments for the interpolation
  *length = i + 1;
}

// function for building interpolated EMRI waveform from python
void get_waveform(cmplx *waveform, double* interp_array,
              double M_phys, double S_phys, double mu, double qS, double phiS, double qK, double phiK, double dist,
              int nmodes, bool mich,
              int init_len, int out_len,
              double delta_t, double *t, int *interval_inds){

    // arrays for determining spline windows for new arrays
    if (init_len > MAX_SPLINE_POINTS)
    {
      char str[1000];
        sprintf(str, "Initial length is greater than the number of maximum allowable spline points: %d > %d", init_len, MAX_SPLINE_POINTS);
        throw_python_error(str, 23);
    }
    #ifdef __CUDACC__
    int num_blocks = std::ceil((out_len + NUM_THREADS -1)/NUM_THREADS);
    dim3 gridDim(num_blocks);

    // launch one worker kernel per stream
    make_waveform<<<gridDim, NUM_THREADS>>>(waveform,
                  interp_array,
                  M_phys, S_phys, mu, qS, phiS, qK, phiK, dist,
                  nmodes, mich,
                  delta_t, t, interval_inds, out_len, init_len);
    cudaDeviceSynchronize();
    gpuErrchk(cudaGetLastError());
    #else

    // CPU waveform generation
    make_waveform(waveform,
                  interp_array,
                  M_phys, S_phys, mu, qS, phiS, qK, phiK, dist,
                  nmodes, mich,
                  delta_t, t, interval_inds, out_len, init_len);
    #endif
}
