.. PolMEM documentation master file, created by
   sphinx-quickstart on Tue Jan 19 15:58:52 2016.
   You can adapt this file completely to your liking, but it should at least
   contain the root `toctree` directive.

PolMEM
==================================
A python package for generating polarimetric VLBI datasets and producing images with a variety of "Maximum Entropy" algorithms.

``vlbi_imaging_utils.py`` contains classes and functions for loading, simulating, and manipulating VLBI data. The three main classes are the :class:`Image`, :class:`Array`, and :class:`Obsdata`. ``maxen.py`` contains a variety of different imaging algorithms for total intensity and polarization that typically take an :class:`Obsdata` object and initial/prior :class:`Image` as input and return another :class:`Image` object.

.. note::
    This is a pre-release version of PolMEM. If you have a problem please submit a pull request on the git repository.

Installation
------------
Download the latest version from the `GitHub repository <https://github.com/achael/eht-imaging>`_
, change to the main directory and run:

.. code-block:: bash

    python setup.py install

You will need working installations of `numpy <http://www.numpy.org/>`_, `matplotlib <http://www.matplotlib.org/>`_,`scipy <http://www.scipy.org/>`_, and `astropy <http://www.astropy.org/>`_.  You can test your installation using the provided :doc:`examples <user/examples>`. 

Quick Guide
-----------

.. toctree::
    :maxdepth: 3
    

Indices and tables
==================

* :ref:`genindex`
* :ref:`modindex`
* :ref:`search`

