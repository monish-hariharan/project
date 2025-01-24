/*
  # Add sample cars

  1. Data
    - Adds 10 sample luxury cars with realistic details
    - Each car has complete information including make, model, year, etc.
    - Images are from Unsplash
*/

INSERT INTO cars (make, model, year, color, daily_rate, image_url, category, seats, transmission, fuel_type) VALUES
  ('Mercedes-Benz', 'S-Class', 2024, 'Black', 299.99, 'https://images.unsplash.com/photo-1622194993940-d29e03e1d3e9', 'Luxury', 5, 'Automatic', 'Hybrid'),
  ('BMW', '7 Series', 2024, 'White', 289.99, 'https://images.unsplash.com/photo-1555215695-3004980ad54e', 'Luxury', 5, 'Automatic', 'Hybrid'),
  ('Porsche', '911', 2024, 'Red', 399.99, 'https://images.unsplash.com/photo-1503376780353-7e6692767b70', 'Sports', 2, 'Automatic', 'Petrol'),
  ('Tesla', 'Model S', 2024, 'Silver', 259.99, 'https://images.unsplash.com/photo-1560958089-b8a1929cea89', 'Electric', 5, 'Automatic', 'Electric'),
  ('Audi', 'RS e-tron GT', 2024, 'Gray', 349.99, 'https://images.unsplash.com/photo-1614200187524-dc4b892acf16', 'Electric', 5, 'Automatic', 'Electric'),
  ('Range Rover', 'Sport', 2024, 'Black', 329.99, 'https://images.unsplash.com/photo-1606664515524-ed2f786a0bd6', 'SUV', 5, 'Automatic', 'Hybrid'),
  ('Lamborghini', 'Huracán', 2024, 'Yellow', 899.99, 'https://images.unsplash.com/photo-1621135802920-133df287f89c', 'Supercar', 2, 'Automatic', 'Petrol'),
  ('Rolls-Royce', 'Ghost', 2024, 'White', 999.99, 'https://images.unsplash.com/photo-1631295868223-63265b40d9e4', 'Ultra Luxury', 5, 'Automatic', 'Petrol'),
  ('Bentley', 'Continental GT', 2024, 'British Racing Green', 799.99, 'https://images.unsplash.com/photo-1632548260498-b7246fa466ea', 'Luxury', 4, 'Automatic', 'Petrol'),
  ('Maserati', 'MC20', 2024, 'Blue', 849.99, 'https://images.unsplash.com/photo-1617814065893-c3c14ae1782d', 'Supercar', 2, 'Automatic', 'Petrol');

-- Add some sample extras
INSERT INTO extras (name, description, daily_rate) VALUES
  ('Insurance Coverage', 'Comprehensive insurance coverage with zero deductible', 49.99),
  ('GPS Navigation', 'Built-in GPS navigation system', 9.99),
  ('Child Seat', 'Safety-certified child car seat', 14.99),
  ('Additional Driver', 'Add an extra authorized driver', 19.99),
  ('Unlimited Mileage', 'No mileage restrictions', 29.99);