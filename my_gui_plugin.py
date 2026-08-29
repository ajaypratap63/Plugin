# my_gui_plugin.py
from abaqusGui import *
from abaqusConstants import *
import my_gui

# Register the plug-in to the Abaqus GUI toolset
toolset = getAFXApp().getAFXMainWindow().getPluginToolset()

toolset.registerGuiMenuButton(
    buttonText='Data Extractor GUI',
    object=my_gui.MyGuiForm(toolset),
    kernelInitString='',
    author='Custom Plugin',
    description='GUI with 3 tabs and a compute button for ODB extraction',
    version='1.0'
)