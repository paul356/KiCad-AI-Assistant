# KiCad Circuit Pattern Recognition Guide

This guide explains how to use the circuit pattern recognition features in the KiCad MCP Server to identify common circuit blocks in your schematics.

## Overview

The circuit pattern recognition functionality allows you to:

1. Automatically identify common circuit patterns in your KiCad schematics
2. Get detailed information about each identified circuit
3. Understand the structure and function of different parts of your design
4. Quickly locate specific circuit types (power supplies, amplifiers, etc.)

## Quick Reference

| Task | Example Prompt |
|------|---------------|
| Identify patterns in a schematic | `Identify circuit patterns in my schematic at /path/to/schematic.kicad_sch` |
| Identify patterns in a project | `Analyze circuit patterns in my KiCad project at /path/to/project.kicad_pro` |
| Get a report of identified patterns | `Show me the circuit patterns in my KiCad project at /path/to/project.kicad_pro` |
| Find specific patterns | `Find all power supply circuits in my schematic at /path/to/schematic.kicad_sch` |

## Using Pattern Recognition Features

### Identifying Circuit Patterns

To identify circuit patterns in a schematic:

```
Identify circuit patterns in my schematic at /path/to/schematic.kicad_sch
```

This will:
- Parse your schematic to extract component and connection information
- Apply pattern recognition algorithms to identify common circuit blocks
- Generate a comprehensive report of all identified patterns
- Provide details about each pattern's components and characteristics

### Project-Based Pattern Recognition

To analyze circuit patterns in a KiCad project:

```
Analyze circuit patterns in my KiCad project at /path/to/project.kicad_pro
```

This will find the schematic associated with your project and perform pattern recognition on it.

### Viewing Pattern Reports

For a formatted report of identified patterns:

```
Show me the circuit patterns in my KiCad project at /path/to/project.kicad_pro
```

This will load the `kicad://patterns/project/path/to/project.kicad_pro` resource, showing:
- A summary of all identified patterns
- Detailed information for each pattern type
- Component references and values
- Additional characteristics specific to each pattern type

### Searching for Specific Pattern Types

You can also ask about specific types of patterns:

```
Find all power supply circuits in my schematic at /path/to/schematic.kicad_sch
```

```
Show me the microcontroller circuits in my KiCad project at /path/to/project.kicad_pro
```

## Supported Pattern Types

The pattern recognition system currently identifies the following types of circuits:

### Power Supply Circuits
- Linear voltage regulators (78xx/79xx series, LDOs, etc.)
- Switching regulators (buck, boost, buck-boost)

### Amplifier Circuits
- Operational amplifiers (general-purpose, audio, instrumentation)
- Transistor amplifiers (BJT, FET)
- Audio amplifier ICs

### Filter Circuits
- Passive filters (RC low-pass/high-pass)
- Active filters (op-amp based)
- Crystal and ceramic filters

### Oscillator Circuits
- Crystal oscillators
- Oscillator ICs
- RC oscillators (555 timer, etc.)

### Digital Interface Circuits
- I2C interfaces
- SPI interfaces
- UART/Serial interfaces
- USB interfaces
- Ethernet interfaces

### Microcontroller Circuits
- Various microcontroller families (AVR, STM32, PIC, ESP, etc.)
- Development boards (Arduino, ESP32, Raspberry Pi Pico, etc.)

### Sensor Interface Circuits
- Temperature sensors
- Humidity sensors
- Pressure sensors
- Motion sensors (accelerometers, gyroscopes)
- Light sensors
- Many other sensor types

## Extending the Pattern Recognition System

The pattern recognition system is designed to be extensible. If you find that certain components or circuit patterns you use frequently aren't being recognized, you can contribute to the system.

### Adding New Component Patterns

The pattern recognition is primarily based on regular expression matching of component values and library IDs. The patterns are defined in the `kcaa/utils/pattern_recognition.py` file.

For example, to add support for a new microcontroller family, you could update the `mcu_patterns` dictionary in the `identify_microcontrollers()` function:

```python
mcu_patterns = {
    # Existing patterns...
    "AVR": r"ATMEGA\d+|ATTINY\d+|AT90\w+",
    "STM32": r"STM32\w+",
    
    # Add your new pattern here
    "Renesas": r"R[A-Z]\d+|RL78|RX\d+",
}
```

Similarly, you can add patterns for new sensors, power supply ICs, or other components in their respective functions.

### Adding New Circuit Recognition Functions

For entirely new types of circuits, you can add new recognition functions in the `kcaa/utils/pattern_recognition.py` file, following the pattern of existing functions.

For example, you might add:

```python
def identify_motor_drivers(components: Dict[str, Any], nets: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Identify motor driver circuits in the schematic."""
    # Your implementation here
    ...
```

Then, update the `identify_circuit_patterns()` function in `kcaa/tools/pattern_tools.py` to call your new function and include its results.

### Contributing Your Extensions

We strongly encourage you to contribute your pattern recognition extensions back to the project so that everyone can benefit from improved recognition capabilities!

To contribute:

1. Fork the repository on GitHub
2. Make your changes to add new patterns or recognition functions
3. Test your changes with your own schematics
4. Submit a pull request with:
   - A description of the new patterns you've added
   - Examples of components/circuits that can now be recognized
   - Any test cases you used to verify the recognition

Your contributions will help build a more comprehensive pattern recognition system that works for a wider variety of designs.

## Troubleshooting

### Patterns Not Being Recognized

If your circuits aren't being recognized:

1. **Check component naming**: The pattern recognition often relies on standard reference designators (R for resistors, C for capacitors, etc.)
2. **Check component values**: Make sure your component values are in standard formats
3. **Check library IDs**: The system also looks at library IDs, so using standard libraries can help
4. **Look at existing patterns**: Check the pattern_recognition.py file to see if your components match the existing patterns

### Pattern Recognition Fails

If the pattern recognition process fails entirely:

1. **Check file paths**: Ensure the schematic file exists and has the correct extension
2. **Verify schematic format**: Make sure it's a valid KiCad 6+ .kicad_sch file
3. **Check file permissions**: Ensure you have read access to the file
4. **Try a simpler schematic**: Start with a small test case to verify functionality

## Advanced Usage

### Integration with Other Features

Combine pattern recognition with other KiCad MCP Server features:

1. **DRC + Pattern Recognition**: Find design issues in specific circuit blocks
   ```
   Find DRC issues affecting the power supply circuits in my schematic
   ```

2. **BOM + Pattern Recognition**: Analyze component usage by circuit type
   ```
   Show me the BOM breakdown for the digital interface circuits in my design
   ```

3. **Netlist + Pattern Recognition**: Understand connectivity in specific patterns
   ```
   Analyze the connections between the microcontroller and sensor interfaces in my design
   ```

### Batch Pattern Recognition

For analyzing multiple projects:

```
Find all projects in my KiCad directory that contain switching regulator circuits
```

```
Compare the digital interfaces used across all my KiCad projects
```

## Future Improvements

We plan to enhance the pattern recognition system with:

1. **More pattern types**: Support for additional circuit patterns
2. **Better connection analysis**: More accurate tracing of connections between components
3. **Hierarchical pattern recognition**: Identifying patterns across hierarchical sheets
4. **Pattern verification**: Validating that recognized patterns follow design best practices
5. **Component suggestions**: Recommending alternative components for recognized patterns

## Contribute to Pattern Recognition

The pattern recognition system relies on a community-driven database of component patterns. The more patterns we have, the better the recognition works for everyone!

If you work with components that aren't being recognized:

1. Check the current patterns in `kcaa/utils/pattern_recognition.py`
2. Add your own patterns for components you use
3. Submit a pull request to share with the community

Common areas where contributions are valuable:
- Modern microcontroller families and variants
- Specialized sensor types
- Power management ICs
- Interface and communication chips
- Industry-specific components

Your expertise in specific component types can help make the pattern recognition more useful for everyone!
